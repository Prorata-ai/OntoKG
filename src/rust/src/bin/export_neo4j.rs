//! Export classified parquet data to CSV files for neo4j-admin database import.
//!
//! Single-pass parallel approach using rayon:
//!   Pass 1: Read each parquet file once (in parallel), writing node + edge CSVs
//!   Pass 2: Write stub nodes (edge targets not in dataset)
//!
//! Usage:
//!   ./export_neo4j
//!   ./export_neo4j --input data_dump_core_output/classified_rust \
//!       --output data_dump_core_output/neo4j --limit 3

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File};
use std::io::BufWriter;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use anyhow::Result;
use arrow::array::{Array, AsArray, ListArray};
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use parking_lot::Mutex;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ProjectionMask;
use rayon::prelude::*;
use tags::TagLoader;

// ── Category → neo4j label mapping ──────────────────────────────────────────

fn category_label(cat: &str) -> &'static str {
    match cat {
        "people" => "PERSON",
        "places" => "PLACE",
        "science" => "SCIENCE",
        "creative_works_media" => "WORK",
        "organizations" => "ORGANIZATION",
        "events_actions" => "EVENT",
        "knowledge_information_abstracts" => "KNOWLEDGE",
        "products_artifacts" => "PRODUCT",
        _ => "ENTITY",
    }
}

fn category_file_label(cat: &str) -> &'static str {
    match cat {
        "people" => "person",
        "places" => "place",
        "science" => "science",
        "creative_works_media" => "work",
        "organizations" => "organization",
        "events_actions" => "event",
        "knowledge_information_abstracts" => "knowledge",
        "products_artifacts" => "product",
        _ => "unknown",
    }
}

// ── CLI ─────────────────────────────────────────────────────────────────────

#[derive(Parser, Debug)]
#[command(name = "export_neo4j")]
#[command(about = "Export classified parquets to CSV for neo4j-admin import")]
struct Args {
    /// Input directory with classified parquet files
    #[arg(long, default_value = "data_dump_core_output/classified_rust")]
    input: PathBuf,

    /// Output directory for CSV files
    #[arg(long, default_value = "data_dump_core_output/neo4j")]
    output: PathBuf,

    /// Directory with YAML tag files
    #[arg(long, default_value = "src/tags")]
    tags: PathBuf,

    /// Limit to first N parquet shards (0 = all)
    #[arg(long, default_value = "0")]
    limit: usize,
}

// ── Core schema: category → [(pid, column_name)] ───────────────────────────

struct CoreSchema {
    pids: BTreeMap<String, Vec<String>>,
    pid_labels: HashMap<String, String>,
}

impl CoreSchema {
    fn columns(&self, cat: &str) -> Vec<(&str, String)> {
        self.pids
            .get(cat)
            .map(|pids| {
                pids.iter()
                    .map(|pid| {
                        let label = self
                            .pid_labels
                            .get(pid)
                            .map(|l| l.as_str())
                            .unwrap_or(pid.as_str());
                        (pid.as_str(), snake_case(label))
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}

fn snake_case(label: &str) -> String {
    let mut result = String::with_capacity(label.len());
    for ch in label.chars() {
        if ch.is_alphanumeric() {
            result.push(ch.to_ascii_lowercase());
        } else if !result.is_empty() && !result.ends_with('_') {
            result.push('_');
        }
    }
    result.trim_end_matches('_').to_string()
}

fn extract_core_pids(config: &tags::CategoryConfig) -> Vec<String> {
    let core = match &config.core {
        Some(value) => value,
        None => return Vec::new(),
    };
    let mapping = match core.as_mapping() {
        Some(map) => map,
        None => return Vec::new(),
    };
    if let Some(props_value) = mapping.get(&serde_yaml::Value::String("props".to_string())) {
        if let Some(seq) = props_value.as_sequence() {
            return seq
                .iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect();
        }
    }
    Vec::new()
}

fn build_schemas(loader: &TagLoader) -> (CoreSchema, Vec<String>) {
    let mut pids = BTreeMap::new();
    let mut relational_modules: HashSet<String> = HashSet::new();

    for (cat_name, config) in &loader.categories {
        pids.insert(cat_name.clone(), extract_core_pids(config));

        for (mod_name, mod_config) in &config.modules {
            if mod_config.module_type.as_deref() == Some("relational") {
                relational_modules.insert(mod_name.clone());
            }
        }
    }

    let mut rel_mods: Vec<String> = relational_modules.into_iter().collect();
    rel_mods.sort();

    let schema = CoreSchema {
        pids,
        pid_labels: HashMap::new(),
    };

    (schema, rel_mods)
}

/// Pre-scan a parquet file to discover PID→label mappings from claims JSON.
fn populate_pid_labels(schema: &mut CoreSchema, parquet_path: &PathBuf) -> Result<()> {
    let file = File::open(parquet_path)?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let parquet_schema = builder.parquet_schema();
    let mask = ProjectionMask::roots(parquet_schema, [5, 11]);
    let reader = builder
        .with_projection(mask)
        .with_batch_size(65536)
        .build()?;

    let mut needed: HashSet<String> = schema
        .pids
        .values()
        .flat_map(|pids| pids.iter().cloned())
        .collect();

    for batch in reader {
        let batch = batch?;
        let n = batch.num_rows();
        let cats = batch.column_by_name("category").unwrap().as_string::<i32>();
        let claims_col = batch.column_by_name("claims").unwrap().as_string::<i32>();

        for i in 0..n {
            if needed.is_empty() {
                break;
            }
            if cats.is_null(i) || claims_col.is_null(i) {
                continue;
            }
            let claims_str = claims_col.value(i);
            if claims_str.is_empty() {
                continue;
            }
            let claims: serde_json::Value = match serde_json::from_str(claims_str) {
                Ok(v) => v,
                Err(_) => continue,
            };
            if let Some(core) = claims.get("core").and_then(|c| c.as_object()) {
                for (pid, data) in core {
                    if needed.contains(pid.as_str()) {
                        if let Some(label) = data.get("label").and_then(|l| l.as_str()) {
                            schema.pid_labels.insert(pid.clone(), label.to_string());
                            needed.remove(pid.as_str());
                        }
                    }
                }
            }
        }
        if needed.is_empty() {
            break;
        }
    }

    Ok(())
}

// ── Helpers ─────────────────────────────────────────────────────────────────

fn is_qid(s: &str) -> bool {
    let bytes = s.as_bytes();
    bytes.len() >= 2 && bytes[0] == b'Q' && bytes[1..].iter().all(|b| b.is_ascii_digit())
}

fn extract_list_values(list: &ListArray, row: usize) -> Vec<String> {
    if list.is_null(row) {
        return Vec::new();
    }
    let values = list.value(row);
    let strings = values.as_string::<i32>();
    let mut result = Vec::with_capacity(strings.len());
    for i in 0..strings.len() {
        if !strings.is_null(i) {
            result.push(strings.value(i).to_string());
        }
    }
    result
}

fn extract_core_values(core_json: &serde_json::Value, pid: &str) -> String {
    let stmts = match core_json.get(pid).and_then(|p| p.get("statements")) {
        Some(serde_json::Value::Array(arr)) => arr,
        _ => return String::new(),
    };
    let mut vals = Vec::new();
    for stmt in stmts {
        if stmt.get("rank").and_then(|r| r.as_str()) == Some("deprecated") {
            continue;
        }
        if let Some(label) = stmt.get("label").and_then(|l| l.as_str()) {
            vals.push(label.to_string());
        } else if let Some(value) = stmt.get("value").and_then(|v| v.as_str()) {
            vals.push(value.to_string());
        }
    }
    vals.join(";")
}

fn format_aliases(json_str: &str) -> String {
    if json_str.is_empty() {
        return String::new();
    }
    match serde_json::from_str::<serde_json::Value>(json_str) {
        Ok(serde_json::Value::Array(arr)) => arr
            .iter()
            .filter_map(|v| v.as_str())
            .collect::<Vec<_>>()
            .join(";"),
        _ => String::new(),
    }
}

fn extract_enwiki(sitelinks_str: &str) -> String {
    if sitelinks_str.is_empty() {
        return String::new();
    }
    match serde_json::from_str::<serde_json::Value>(sitelinks_str) {
        Ok(obj) => obj
            .get("enwiki")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        _ => String::new(),
    }
}

fn build_labels(cat_label: &str, intrinsic_mods: &[String], relational_mods: &[String]) -> String {
    let mut parts = vec!["ENTITY".to_string(), cat_label.to_string()];
    for m in intrinsic_mods {
        parts.push(m.to_uppercase());
    }
    for m in relational_mods {
        parts.push(m.to_uppercase());
    }
    parts.join(";")
}

fn format_qualifiers(qualifiers: &serde_json::Value) -> String {
    let obj = match qualifiers.as_object() {
        Some(o) => o,
        None => return String::new(),
    };
    let mut parts = Vec::new();
    for (_pid, data) in obj {
        let label = data
            .get("label")
            .and_then(|l| l.as_str())
            .unwrap_or(_pid.as_str());
        let vals: Vec<&str> = data
            .get("values")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| {
                        v.get("label")
                            .and_then(|l| l.as_str())
                            .or_else(|| v.get("value").and_then(|val| val.as_str()))
                    })
                    .collect()
            })
            .unwrap_or_default();
        if !vals.is_empty() {
            parts.push(format!("{}: {}", label, vals.join(", ")));
        }
    }
    parts.join("; ")
}

// ── Main ────────────────────────────────────────────────────────────────────

fn main() -> Result<()> {
    let args = Args::parse();
    let t_start = std::time::Instant::now();

    // Discover parquet files
    let mut parquet_files: Vec<PathBuf> = fs::read_dir(&args.input)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "parquet"))
        .collect();
    parquet_files.sort();

    if parquet_files.is_empty() {
        anyhow::bail!("No parquet files found in {:?}", args.input);
    }

    if args.limit > 0 {
        parquet_files.truncate(args.limit);
    }

    fs::create_dir_all(&args.output)?;

    println!("Input:  {:?}", args.input);
    println!("Output: {:?}", args.output);
    println!("Files:  {}", parquet_files.len());

    // Load YAML tags
    let loader = TagLoader::new(Some(args.tags.clone()))?;
    let (mut core_schema, relational_modules) = build_schemas(&loader);

    // Pre-scan first parquet file to discover PID labels from claims JSON
    populate_pid_labels(&mut core_schema, &parquet_files[0])?;

    let categories: Vec<String> = loader.get_category_names();
    println!("Categories: {}", categories.len());
    println!("Relational modules: {}", relational_modules.len());

    // Pre-compute per-category core columns (avoid recomputation per entity)
    let core_columns: HashMap<String, Vec<(String, String)>> = categories
        .iter()
        .map(|cat| {
            let cols = core_schema
                .columns(cat)
                .into_iter()
                .map(|(pid, col)| (pid.to_string(), col))
                .collect();
            (cat.clone(), cols)
        })
        .collect();

    // Pre-compute uppercase module names
    let mod_upper: HashMap<String, String> = relational_modules
        .iter()
        .map(|m| (m.clone(), m.to_uppercase()))
        .collect();

    // ── Open Mutex-wrapped CSV writers ──────────────────────────────────

    let mut node_writers: HashMap<String, Mutex<csv::Writer<BufWriter<File>>>> = HashMap::new();
    let node_counts: HashMap<String, AtomicU64> = categories
        .iter()
        .map(|cat| (cat.clone(), AtomicU64::new(0)))
        .collect();

    for cat in &categories {
        let cat_label = category_file_label(cat);
        let path = args.output.join(format!("nodes_{}.csv", cat_label));
        let file = BufWriter::with_capacity(1 << 20, File::create(&path)?);
        let mut wtr = csv::Writer::from_writer(file);

        let mut header = vec![
            ":ID".to_string(),
            "qid".to_string(),
            "label".to_string(),
            "description".to_string(),
            "aliases:string[]".to_string(),
            "enwiki".to_string(),
        ];
        if let Some(cols) = core_columns.get(cat) {
            for (_, col_name) in cols {
                header.push(col_name.clone());
            }
        }
        header.push(":LABEL".to_string());
        wtr.write_record(&header)?;
        node_writers.insert(cat.clone(), Mutex::new(wtr));
    }

    let mut edge_writers: HashMap<String, Mutex<csv::Writer<BufWriter<File>>>> = HashMap::new();
    let edge_counts: HashMap<String, AtomicU64> = relational_modules
        .iter()
        .map(|m| (m.clone(), AtomicU64::new(0)))
        .collect();

    for module in &relational_modules {
        let path = args.output.join(format!("edges_{}.csv", module));
        let file = BufWriter::with_capacity(1 << 20, File::create(&path)?);
        let mut wtr = csv::Writer::from_writer(file);
        wtr.write_record([":START_ID", ":END_ID", ":TYPE", "property", "pid", "qualifiers"])?;
        edge_writers.insert(module.clone(), Mutex::new(wtr));
    }

    let all_qids: Mutex<HashSet<String>> = Mutex::new(HashSet::with_capacity(35_000_000));
    let edge_targets: Mutex<HashSet<String>> = Mutex::new(HashSet::new());

    // ── Single parallel pass: nodes + edges ─────────────────────────────

    println!("\n── Processing parquet files ({} threads) ──", rayon::current_num_threads());

    let progress = ProgressBar::new(parquet_files.len() as u64);
    progress.set_style(ProgressStyle::default_bar().template(
        "[{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} files ({per_sec}, ETA {eta})",
    )?);

    parquet_files.par_iter().try_for_each(|path| -> Result<()> {
        let file = File::open(path)?;
        let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
        let parquet_schema = builder.parquet_schema();
        // All columns needed: qid(0), label(1), description(2), aliases(3),
        // category(5), intrinsic_modules(6), relational_modules(7), claims(11), sitelinks(12)
        let mask = ProjectionMask::roots(parquet_schema, [0, 1, 2, 3, 5, 6, 7, 11, 12]);
        let reader = builder
            .with_projection(mask)
            .with_batch_size(65536)
            .build()?;

        let mut local_qids: HashSet<String> = HashSet::with_capacity(1_500_000);
        let mut local_edge_targets: HashSet<String> = HashSet::with_capacity(500_000);

        for batch in reader {
            let batch = batch?;
            let n = batch.num_rows();

            let qids = batch.column_by_name("qid").unwrap().as_string::<i32>();
            let labels = batch.column_by_name("label").unwrap().as_string::<i32>();
            let descs = batch
                .column_by_name("description")
                .unwrap()
                .as_string::<i32>();
            let aliases_col = batch.column_by_name("aliases").unwrap().as_string::<i32>();
            let cats = batch
                .column_by_name("category")
                .unwrap()
                .as_string::<i32>();
            let i_mods = batch
                .column_by_name("intrinsic_modules")
                .unwrap()
                .as_any()
                .downcast_ref::<ListArray>()
                .unwrap();
            let r_mods = batch
                .column_by_name("relational_modules")
                .unwrap()
                .as_any()
                .downcast_ref::<ListArray>()
                .unwrap();
            let claims_col = batch.column_by_name("claims").unwrap().as_string::<i32>();
            let sitelinks_col = batch
                .column_by_name("sitelinks")
                .unwrap()
                .as_string::<i32>();

            for i in 0..n {
                let is_classified = !cats.is_null(i);
                let rel_mods_list = extract_list_values(r_mods, i);
                let need_claims = is_classified || !rel_mods_list.is_empty();

                // Parse claims JSON once for both node and edge extraction
                let claims: serde_json::Value = if need_claims {
                    let claims_str = if claims_col.is_null(i) {
                        ""
                    } else {
                        claims_col.value(i)
                    };
                    if claims_str.is_empty() {
                        serde_json::Value::Null
                    } else {
                        serde_json::from_str(claims_str).unwrap_or(serde_json::Value::Null)
                    }
                } else {
                    serde_json::Value::Null
                };

                // === Node writing ===
                if is_classified {
                    let qid = qids.value(i);
                    local_qids.insert(qid.to_string());
                    let cat = cats.value(i);

                    if let Some(wtr_mutex) = node_writers.get(cat) {
                        let core_json =
                            claims.get("core").unwrap_or(&serde_json::Value::Null);

                        let label = if labels.is_null(i) {
                            ""
                        } else {
                            labels.value(i)
                        };
                        let desc = if descs.is_null(i) { "" } else { descs.value(i) };
                        let aliases_str = if aliases_col.is_null(i) {
                            ""
                        } else {
                            aliases_col.value(i)
                        };
                        let sitelinks_str = if sitelinks_col.is_null(i) {
                            ""
                        } else {
                            sitelinks_col.value(i)
                        };

                        let aliases = format_aliases(aliases_str);
                        let enwiki = extract_enwiki(sitelinks_str);

                        let intrinsic_mods = extract_list_values(i_mods, i);
                        let cat_neo4j = category_label(cat);
                        let label_col =
                            build_labels(cat_neo4j, &intrinsic_mods, &rel_mods_list);

                        let mut record = vec![
                            qid.to_string(),
                            qid.to_string(),
                            label.to_string(),
                            desc.to_string(),
                            aliases,
                            enwiki,
                        ];

                        if let Some(cols) = core_columns.get(cat) {
                            for (pid, _) in cols {
                                record.push(extract_core_values(core_json, pid));
                            }
                        }

                        record.push(label_col);
                        wtr_mutex.lock().write_record(&record)?;

                        if let Some(count) = node_counts.get(cat) {
                            count.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }

                // === Edge writing ===
                if !rel_mods_list.is_empty() {
                    if let Some(relational) = claims.get("relational") {
                        let qid = qids.value(i);

                        for module in &rel_mods_list {
                            let mod_data = match relational.get(module.as_str()) {
                                Some(v) => v,
                                None => continue,
                            };
                            let mod_obj = match mod_data.as_object() {
                                Some(o) => o,
                                None => continue,
                            };

                            let module_upper = match mod_upper.get(module) {
                                Some(u) => u.as_str(),
                                None => continue,
                            };

                            let wtr_mutex = match edge_writers.get(module.as_str()) {
                                Some(w) => w,
                                None => continue,
                            };

                            for (pid, prop_data) in mod_obj {
                                let prop_label = prop_data
                                    .get("label")
                                    .and_then(|l| l.as_str())
                                    .unwrap_or(pid.as_str());

                                let stmts =
                                    match prop_data.get("statements").and_then(|s| s.as_array()) {
                                        Some(arr) => arr,
                                        None => continue,
                                    };

                                for stmt in stmts {
                                    if stmt.get("rank").and_then(|r| r.as_str())
                                        == Some("deprecated")
                                    {
                                        continue;
                                    }
                                    let target =
                                        match stmt.get("value").and_then(|v| v.as_str()) {
                                            Some(v) if is_qid(v) => v,
                                            _ => continue,
                                        };

                                    let quals = stmt
                                        .get("qualifiers")
                                        .map(|q| format_qualifiers(q))
                                        .unwrap_or_default();

                                    wtr_mutex.lock().write_record(&[
                                        qid,
                                        target,
                                        module_upper,
                                        prop_label,
                                        pid.as_str(),
                                        &quals,
                                    ])?;

                                    local_edge_targets.insert(target.to_string());

                                    if let Some(count) = edge_counts.get(module.as_str()) {
                                        count.fetch_add(1, Ordering::Relaxed);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // Batch merge local sets into shared state (one lock per file)
        all_qids.lock().extend(local_qids);
        edge_targets.lock().extend(local_edge_targets);

        progress.inc(1);
        Ok(())
    })?;

    progress.finish_and_clear();

    // Flush all writers
    for (_, wtr) in node_writers {
        wtr.into_inner().flush()?;
    }
    for (_, wtr) in edge_writers {
        wtr.into_inner().flush()?;
    }

    // Collect stats
    let total_nodes: u64 = node_counts.values().map(|c| c.load(Ordering::Relaxed)).sum();
    let total_edges: u64 = edge_counts.values().map(|c| c.load(Ordering::Relaxed)).sum();

    println!(
        "  {} nodes, {} edges in {:.1}s",
        total_nodes,
        total_edges,
        t_start.elapsed().as_secs_f64()
    );

    for cat in &categories {
        let count = node_counts
            .get(cat)
            .map(|c| c.load(Ordering::Relaxed))
            .unwrap_or(0);
        if count > 0 {
            println!(
                "  nodes_{}.csv: {} nodes",
                category_file_label(cat),
                count
            );
        }
    }
    for module in &relational_modules {
        let count = edge_counts
            .get(module)
            .map(|c| c.load(Ordering::Relaxed))
            .unwrap_or(0);
        if count > 0 {
            println!("  edges_{}.csv: {} edges", module, count);
        }
    }

    // ── Stub pass ───────────────────────────────────────────────────────

    println!("\n── Writing stubs ──");
    let t_stubs = std::time::Instant::now();

    let all_qids = all_qids.into_inner();
    let edge_targets = edge_targets.into_inner();

    let mut stubs: Vec<String> = edge_targets
        .into_iter()
        .filter(|qid| !all_qids.contains(qid))
        .collect();
    stubs.sort();

    let stub_path = args.output.join("nodes_stub.csv");
    let stub_file = BufWriter::new(File::create(&stub_path)?);
    let mut stub_wtr = csv::Writer::from_writer(stub_file);
    stub_wtr.write_record([":ID", "qid", "label", ":LABEL"])?;

    for qid in &stubs {
        stub_wtr.write_record([qid.as_str(), qid.as_str(), "", "ENTITY"])?;
    }
    stub_wtr.flush()?;

    println!(
        "  {} stubs in {:.1}s",
        stubs.len(),
        t_stubs.elapsed().as_secs_f64()
    );

    // ── Summary ─────────────────────────────────────────────────────────

    println!("\n── Summary ──");
    println!("  Total nodes: {}", total_nodes);
    println!("  Total edges: {}", total_edges);
    println!("  Total stubs: {}", stubs.len());
    println!("  Total time:  {:.1}s", t_start.elapsed().as_secs_f64());

    // Print import command
    println!("\nImport command:");
    let mut cmd_parts = vec!["neo4j-admin database import full".to_string()];
    for cat in &categories {
        let short = category_file_label(cat);
        cmd_parts.push(format!("  --nodes=nodes_{}.csv", short));
    }
    if !stubs.is_empty() {
        cmd_parts.push("  --nodes=nodes_stub.csv".to_string());
    }
    for module in &relational_modules {
        let count = edge_counts
            .get(module)
            .map(|c| c.load(Ordering::Relaxed))
            .unwrap_or(0);
        if count > 0 {
            cmd_parts.push(format!("  --relationships=edges_{}.csv", module));
        }
    }
    cmd_parts.push("  --delimiter=','".to_string());
    cmd_parts.push("  --array-delimiter=';'".to_string());
    cmd_parts.push("  --skip-bad-relationships=true".to_string());
    cmd_parts.push("  --overwrite-destination=true".to_string());
    cmd_parts.push("  neo4j".to_string());
    println!("{}", cmd_parts.join(" \\\n"));

    Ok(())
}
