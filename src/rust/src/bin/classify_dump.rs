//! Classify Wikidata dump entities and output to Parquet.
//! Uses LMDB to filter entities with English labels only.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use anyhow::Result;
use arrow::array::{ArrayRef, ListBuilder, StringBuilder};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use clap::Parser;
use crossbeam_channel::{bounded, Receiver, Sender};
use heed::types::Str;
use heed::{Database, EnvOpenOptions};
use indicatif::{MultiProgress, ProgressBar, ProgressStyle};
use parking_lot::Mutex;
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::WriterProperties;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use simd_json::prelude::*;
use tags::{CategoryConfig, Entity, TagLoader};

#[derive(Parser, Debug)]
#[command(name = "classify_dump")]
#[command(about = "Classify Wikidata dump entities to Parquet")]
struct Args {
    /// Directory with .jsonl.zst files
    #[arg(long, default_value = "data_dump_core")]
    dump_dir: PathBuf,

    /// LMDB database for label lookup
    #[arg(long, default_value = "data_dump_output/labels_en.lmdb")]
    lmdb: PathBuf,

    /// Directory with tag YAML files
    #[arg(long, default_value = "src/tags")]
    tags_dir: PathBuf,

    /// Output directory
    #[arg(long, default_value = "data_dump_core_output/classified_rust")]
    output: PathBuf,

    /// Number of files to process in parallel
    #[arg(long, default_value = "4")]
    file_workers: usize,

    /// Number of workers per file
    #[arg(long, default_value = "16")]
    workers: usize,

    /// Process only first N files (0 = all). Ignored if --files or --start/--end specified.
    #[arg(long, default_value = "0")]
    max_files: usize,

    /// Restart from beginning, ignore checkpoint
    #[arg(long)]
    restart: bool,

    /// Force reprocess selected files even if already completed (doesn't clear other checkpoint entries)
    #[arg(long)]
    force: bool,

    /// Specific file IDs to process (comma-separated, e.g., "0001,0003,0007")
    #[arg(long, value_delimiter = ',')]
    files: Option<Vec<String>>,

    /// Start file number (inclusive, e.g., 5 for _0005)
    #[arg(long)]
    start: Option<usize>,

    /// End file number (inclusive, e.g., 10 for _0010)
    #[arg(long)]
    end: Option<usize>,
}

/// Checkpoint for resumability
#[derive(Debug, Default, Serialize, Deserialize)]
struct Checkpoint {
    completed: HashMap<String, bool>,
    #[serde(default)]
    in_progress: Vec<String>,
}

/// Classified entity for Parquet output
#[derive(Debug, Clone)]
struct ClassifiedEntity {
    qid: String,
    label: String,
    description: Option<String>,
    aliases: Option<String>, // JSON array
    p31_values: Vec<String>,
    category: Option<String>,
    intrinsic_modules: Vec<String>,
    relational_modules: Vec<String>,
    gate_matched: Option<String>,
    gate_property: Option<String>,
    matched_indicators: Option<String>, // JSON
    claims: String,                     // Full raw claims JSON
    sitelinks: Option<String>,          // Full raw sitelinks JSON
}

/// Stats for a processed file
#[derive(Debug, Default)]
struct FileStats {
    total_records: u64,
    items: u64,
    with_label: u64,
    classified: u64,
    with_modules: u64,
    unclassified: u64,
}

/// Per-worker label cache wrapping LMDB lookups.
struct LabelCache {
    cache: HashMap<String, Option<String>>,
}

impl LabelCache {
    fn new() -> Self {
        Self {
            cache: HashMap::new(),
        }
    }

    fn get(&mut self, rtxn: &heed::RoTxn, db: &Database<Str, Str>, qid: &str) -> Option<String> {
        if let Some(cached) = self.cache.get(qid) {
            return cached.clone();
        }
        let val = db
            .get(rtxn, qid)
            .ok()
            .flatten()
            .map(|s| s.to_string());
        let result = val.clone();
        self.cache.insert(qid.to_string(), val);
        result
    }
}

/// Pre-computed PID sets for a category's core and modules.
struct CategoryPids {
    core_pids: HashSet<String>,
    module_pids: HashMap<String, HashSet<String>>, // module_name -> set of PIDs
    module_types: HashMap<String, String>,          // module_name -> "intrinsic" | "relational"
}

impl CategoryPids {
    fn from_config(config: &CategoryConfig) -> Self {
        let core_pids = extract_core_pids(config);
        let mut module_pids = HashMap::new();
        let mut module_types = HashMap::new();
        for (mod_name, mod_config) in &config.modules {
            let mut pids: HashSet<String> = mod_config.indicators.keys().cloned().collect();
            pids.extend(mod_config.value_props.iter().cloned());
            module_pids.insert(mod_name.clone(), pids);
            module_types.insert(
                mod_name.clone(),
                mod_config.module_type.clone().unwrap_or_else(|| "intrinsic".to_string()),
            );
        }
        Self {
            core_pids,
            module_pids,
            module_types,
        }
    }

    /// Return pid -> set of module names for the given matched modules.
    fn get_module_pids(&self, modules: &[String]) -> BTreeMap<String, BTreeSet<String>> {
        let mut result: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for mod_name in modules {
            if let Some(pids) = self.module_pids.get(mod_name) {
                for pid in pids {
                    result.entry(pid.clone()).or_default().insert(mod_name.clone());
                }
            }
        }
        result
    }
}

/// Extract core PIDs from a CategoryConfig's YAML core section.
fn extract_core_pids(config: &CategoryConfig) -> HashSet<String> {
    let mut pids = HashSet::new();
    let core = match &config.core {
        Some(value) => value,
        None => return pids,
    };
    let mapping = match core.as_mapping() {
        Some(map) => map,
        None => return pids,
    };
    if let Some(props_value) =
        mapping.get(&serde_yaml::Value::String("props".to_string()))
    {
        if let Some(seq) = props_value.as_sequence() {
            for value in seq {
                if let Some(prop) = value.as_str() {
                    pids.insert(prop.to_string());
                }
            }
        }
        return pids;
    }
    for value in mapping.values() {
        if let Some(seq) = value.as_sequence() {
            for item in seq {
                if let Some(prop) = item.as_str() {
                    pids.insert(prop.to_string());
                }
            }
        }
    }
    pids
}

fn main() -> Result<()> {
    let args = Args::parse();

    // List dump files
    let mut files: Vec<PathBuf> = fs::read_dir(&args.dump_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().map_or(false, |e| e == "zst"))
        .collect();
    files.sort();

    if files.is_empty() {
        eprintln!("No .jsonl.zst files found in {:?}", args.dump_dir);
        std::process::exit(1);
    }

    // Filter files based on --files, --start/--end, or --max-files
    files = filter_files(files, &args);

    // Setup output directory
    fs::create_dir_all(&args.output)?;

    // Load checkpoint
    let checkpoint_path = args.output.join("checkpoint.json");
    let checkpoint = load_checkpoint(&checkpoint_path, args.restart);
    let checkpoint = Arc::new(Mutex::new(checkpoint));

    // Open LMDB for label lookup
    let lmdb_env = unsafe {
        EnvOpenOptions::new()
            .max_dbs(1)
            .map_size(20 * 1024 * 1024 * 1024) // 20GB
            .open(&args.lmdb)?
    };
    let lmdb_db: Database<Str, Str> = {
        let rtxn = lmdb_env.read_txn()?;
        lmdb_env.open_database(&rtxn, Some("labels_en"))?.unwrap()
    };
    let lmdb_env = Arc::new(lmdb_env);
    let lmdb_db = Arc::new(lmdb_db);

    // Load tag classifier
    let loader = TagLoader::new(Some(args.tags_dir.clone()))?;
    let loader = Arc::new(loader);

    // Build gate QID lookup
    let gate_qid_to_category = build_gate_qid_lookup(&loader);
    let gate_qid_to_category = Arc::new(gate_qid_to_category);

    // Pre-compute category PID sets for claim organization
    let cat_pids_map: HashMap<String, CategoryPids> = loader
        .categories
        .iter()
        .map(|(name, config)| (name.clone(), CategoryPids::from_config(config)))
        .collect();
    let cat_pids_map = Arc::new(cat_pids_map);

    // Calculate total bytes
    let total_bytes: u64 = files
        .iter()
        .filter_map(|f| fs::metadata(f).ok())
        .map(|m| m.len())
        .sum();

    println!("Dump directory: {:?}", args.dump_dir);
    println!("LMDB: {:?}", args.lmdb);
    println!("Output: {:?}", args.output);
    println!("Files: {}", files.len());
    println!(
        "File workers: {}, Workers per file: {}",
        args.file_workers, args.workers
    );
    println!("Total compressed: {}", human_bytes(total_bytes));

    // Filter files to process (skip completed unless --force)
    let files_to_process: Vec<PathBuf> = {
        let cp = checkpoint.lock();
        files
            .iter()
            .filter(|f| {
                let file_id = extract_file_id(f);
                let already_done = cp.completed.contains_key(&file_id);
                if already_done && args.force {
                    println!("Force reprocessing: {}", file_id);
                    true
                } else {
                    !already_done
                }
            })
            .cloned()
            .collect()
    };

    if files_to_process.is_empty() {
        println!("All files already processed. Use --restart to reprocess.");
        return Ok(());
    }

    println!(
        "Files to process: {} (skipping {} completed)",
        files_to_process.len(),
        files.len() - files_to_process.len()
    );

    // Clean up incomplete files
    {
        let mut cp = checkpoint.lock();
        for file_id in &cp.in_progress {
            let incomplete_path = args.output.join(format!("{}.parquet", file_id));
            if incomplete_path.exists() {
                println!("Removing incomplete file: {:?}", incomplete_path);
                let _ = fs::remove_file(&incomplete_path);
            }
        }
        cp.in_progress.clear();
        save_checkpoint(&checkpoint_path, &cp)?;
    }

    // Progress bar
    let multi_progress = MultiProgress::new();
    let overall_progress = multi_progress.add(ProgressBar::new(total_bytes));
    overall_progress.set_style(
        ProgressStyle::default_bar()
            .template("[{elapsed_precise}] [{bar:40.cyan/blue}] {bytes}/{total_bytes} ({bytes_per_sec}, ETA {eta})")?
    );

    let bytes_read = Arc::new(AtomicU64::new(0));

    // Process files in parallel
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(args.file_workers)
        .build()?;

    let results: Vec<Result<(String, FileStats)>> = pool.install(|| {
        files_to_process
            .par_iter()
            .map(|file| {
                let file_id = extract_file_id(file);

                // Mark as in progress
                {
                    let mut cp = checkpoint.lock();
                    cp.in_progress.push(file_id.clone());
                    let _ = save_checkpoint(&checkpoint_path, &cp);
                }

                let result = process_file(
                    file,
                    &args.output,
                    &lmdb_env,
                    &lmdb_db,
                    &loader,
                    &gate_qid_to_category,
                    &cat_pids_map,
                    args.workers,
                    &bytes_read,
                    &overall_progress,
                );

                match result {
                    Ok(stats) => {
                        // Mark as completed
                        let mut cp = checkpoint.lock();
                        cp.completed.insert(file_id.clone(), true);
                        cp.in_progress.retain(|id| id != &file_id);
                        let _ = save_checkpoint(&checkpoint_path, &cp);

                        println!(
                            "[{}] records={} items={} labeled={} classified={} modules={} unclassified={}",
                            file_id, stats.total_records, stats.items, stats.with_label,
                            stats.classified, stats.with_modules, stats.unclassified
                        );

                        Ok((file_id, stats))
                    }
                    Err(e) => Err(e),
                }
            })
            .collect()
    });

    overall_progress.finish_with_message("Done");

    // Aggregate stats
    let mut total_stats = FileStats::default();
    for result in results {
        match result {
            Ok((_, stats)) => {
                total_stats.total_records += stats.total_records;
                total_stats.items += stats.items;
                total_stats.with_label += stats.with_label;
                total_stats.classified += stats.classified;
                total_stats.with_modules += stats.with_modules;
                total_stats.unclassified += stats.unclassified;
            }
            Err(e) => {
                eprintln!("Error: {}", e);
            }
        }
    }

    println!(
        "\nTOTAL: records={} items={} labeled={} classified={} modules={} unclassified={}",
        total_stats.total_records,
        total_stats.items,
        total_stats.with_label,
        total_stats.classified,
        total_stats.with_modules,
        total_stats.unclassified
    );

    Ok(())
}

fn process_file(
    file_path: &PathBuf,
    out_dir: &PathBuf,
    lmdb_env: &Arc<heed::Env>,
    _lmdb_db: &Arc<Database<Str, Str>>,
    loader: &Arc<TagLoader>,
    gate_qid_to_category: &Arc<HashMap<String, String>>,
    cat_pids_map: &Arc<HashMap<String, CategoryPids>>,
    worker_count: usize,
    bytes_read: &Arc<AtomicU64>,
    progress: &ProgressBar,
) -> Result<FileStats> {
    let file_id = extract_file_id(file_path);
    let out_path = out_dir.join(format!("{}.parquet", file_id));

    // Open and decompress file
    let file = File::open(file_path)?;
    let file_size = file.metadata()?.len();
    let decoder = zstd::Decoder::new(file)?;
    let reader = BufReader::with_capacity(1024 * 1024, decoder);

    // Channels for pipeline
    let (line_tx, line_rx): (Sender<String>, Receiver<String>) = bounded(worker_count * 100);
    let (result_tx, result_rx): (Sender<ClassifiedEntity>, Receiver<ClassifiedEntity>) =
        bounded(worker_count * 100);

    // Stats
    let stats = Arc::new(Mutex::new(FileStats::default()));

    // Reader thread
    let reader_stats = stats.clone();
    let reader_handle = std::thread::spawn(move || {
        for line in reader.lines() {
            match line {
                Ok(line) => {
                    reader_stats.lock().total_records += 1;
                    if line_tx.send(line).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        drop(line_tx);
    });

    // Worker threads - each worker gets its own LMDB read transaction (reused)
    let worker_handles: Vec<_> = (0..worker_count)
        .map(|_| {
            let line_rx = line_rx.clone();
            let result_tx = result_tx.clone();
            let lmdb_env = lmdb_env.clone();
            let loader = loader.clone();
            let gate_qid_to_category = gate_qid_to_category.clone();
            let cat_pids_map = cat_pids_map.clone();
            let stats = stats.clone();

            std::thread::spawn(move || {
                // Create per-worker read transaction (reused for all entities)
                let rtxn = match lmdb_env.read_txn() {
                    Ok(txn) => txn,
                    Err(e) => {
                        eprintln!("Failed to create read txn: {:?}", e);
                        return;
                    }
                };

                // Re-open database handle in this thread's transaction
                let db: Database<Str, Str> = match lmdb_env.open_database(&rtxn, Some("labels_en"))
                {
                    Ok(Some(db)) => db,
                    Ok(None) => {
                        eprintln!("Database not found in worker");
                        return;
                    }
                    Err(e) => {
                        eprintln!("Failed to open db in worker: {:?}", e);
                        return;
                    }
                };

                // Per-worker label cache (accumulates across all entities in batch)
                let mut label_cache = LabelCache::new();

                for line in line_rx {
                    if let Some(entity) = process_entity_with_txn(
                        &line,
                        &rtxn,
                        &db,
                        &mut label_cache,
                        &loader,
                        &gate_qid_to_category,
                        &cat_pids_map,
                        &stats,
                    ) {
                        if result_tx.send(entity).is_err() {
                            break;
                        }
                    }
                }
                // rtxn, db, label_cache dropped here
            })
        })
        .collect();

    drop(line_rx);
    drop(result_tx);

    // Writer thread
    let writer_handle = std::thread::spawn(move || -> Result<()> {
        let schema = create_parquet_schema();
        let file = File::create(&out_path)?;
        let props = WriterProperties::builder()
            .set_compression(Compression::ZSTD(Default::default()))
            .build();
        let mut writer = ArrowWriter::try_new(file, schema.clone(), Some(props))?;

        let mut batch: Vec<ClassifiedEntity> = Vec::with_capacity(10000);

        for entity in result_rx {
            batch.push(entity);
            if batch.len() >= 10000 {
                write_batch(&mut writer, &schema, &batch)?;
                batch.clear();
            }
        }

        if !batch.is_empty() {
            write_batch(&mut writer, &schema, &batch)?;
        }

        writer.close()?;
        Ok(())
    });

    // Wait for reader
    reader_handle.join().unwrap();

    // Wait for workers
    for handle in worker_handles {
        handle.join().unwrap();
    }

    // Wait for writer
    writer_handle.join().unwrap()?;

    // Update progress
    bytes_read.fetch_add(file_size, Ordering::Relaxed);
    progress.set_position(bytes_read.load(Ordering::Relaxed));

    let final_stats = stats.lock().clone();
    Ok(final_stats)
}

fn process_entity_with_txn(
    line: &str,
    rtxn: &heed::RoTxn,
    lmdb_db: &Database<Str, Str>,
    label_cache: &mut LabelCache,
    loader: &TagLoader,
    gate_qid_to_category: &HashMap<String, String>,
    cat_pids_map: &HashMap<String, CategoryPids>,
    stats: &Arc<Mutex<FileStats>>,
) -> Option<ClassifiedEntity> {
    // Parse JSON using simd-json for speed
    let mut line_bytes = line.as_bytes().to_vec();
    let value: simd_json::OwnedValue = simd_json::to_owned_value(&mut line_bytes).ok()?;

    let obj = value.as_object()?;

    // Check type
    let entity_type = obj.get("type")?.as_str()?;
    if entity_type != "item" {
        return None;
    }

    let qid = obj.get("id")?.as_str()?.to_string();
    if qid.is_empty() {
        return None;
    }

    stats.lock().items += 1;

    // Check LMDB for English label (reuse transaction)
    let label = match lmdb_db.get(rtxn, &qid).ok().flatten() {
        Some(l) => l.to_string(),
        None => return None,
    };

    stats.lock().with_label += 1;

    // Extract description with language fallback
    let description = obj.get("descriptions").and_then(|d| resolve_lang(d));

    // Extract English aliases as JSON array
    let aliases = extract_aliases(obj);

    // Extract flattened properties for classification
    let claims_value = obj.get("claims")?;
    let props = extract_properties(claims_value)?;

    // Classify using flattened properties
    let entity = Entity {
        qid: qid.clone(),
        properties: props.clone(),
    };
    let result = loader.classify(&entity);

    // Build output
    let p31_values = props.get("P31").cloned().unwrap_or_default();

    let (category, gate_matched, gate_property, matched_indicators) = if !result.category.is_empty()
    {
        stats.lock().classified += 1;

        // Find gate matched: use P31 or P279 depending on gate_property
        let gate = if result.gate_property == "P279" {
            props
                .get("P279")
                .and_then(|vals| {
                    vals.iter().find(|v| {
                        gate_qid_to_category
                            .get(*v)
                            .map_or(false, |c| c == &result.category)
                    })
                })
                .cloned()
        } else {
            p31_values
                .iter()
                .find(|p31| {
                    gate_qid_to_category
                        .get(*p31)
                        .map_or(false, |c| c == &result.category)
                })
                .cloned()
        };

        let gp = if result.gate_property.is_empty() {
            None
        } else {
            Some(result.gate_property.clone())
        };

        let indicators = if !result.modules.is_empty() {
            stats.lock().with_modules += 1;
            let ind = build_matched_indicators(&props, loader, &result.category, &result.modules);
            Some(serde_json::to_string(&ind).unwrap_or_default())
        } else {
            None
        };

        (Some(result.category), gate, gp, indicators)
    } else {
        stats.lock().unclassified += 1;
        (None, None, None, None)
    };

    // Organize claims into core/modules/unclaimed buckets with labels
    let claims_json = organize_claims(
        claims_value,
        category.as_deref(),
        &result.modules,
        cat_pids_map,
        rtxn,
        lmdb_db,
        label_cache,
        &label,
    );

    // Extract enwiki sitelink URL only
    let sitelinks = obj
        .get("sitelinks")
        .and_then(|s| s.as_object())
        .and_then(|s| s.get("enwiki"))
        .and_then(|e| e.as_object())
        .and_then(|e| e.get("title"))
        .and_then(|t| t.as_str())
        .map(|title| {
            let encoded = title.replace(' ', "_");
            let url = format!("https://en.wikipedia.org/wiki/{}", encoded);
            serde_json::json!({"enwiki": url}).to_string()
        });

    // Split modules by type
    let (intrinsic_modules, relational_modules) = if let Some(ref cat) = category {
        if let Some(cat_pids) = cat_pids_map.get(cat) {
            let mut intrinsic = Vec::new();
            let mut relational = Vec::new();
            for m in &result.modules {
                match cat_pids.module_types.get(m).map(|s| s.as_str()) {
                    Some("relational") => relational.push(m.clone()),
                    _ => intrinsic.push(m.clone()),
                }
            }
            (intrinsic, relational)
        } else {
            (Vec::new(), Vec::new())
        }
    } else {
        (Vec::new(), Vec::new())
    };

    Some(ClassifiedEntity {
        qid,
        label,
        description,
        aliases,
        p31_values,
        category,
        intrinsic_modules,
        relational_modules,
        gate_matched,
        gate_property,
        matched_indicators,
        claims: claims_json,
        sitelinks,
    })
}

fn extract_properties(claims: &simd_json::OwnedValue) -> Option<HashMap<String, Vec<String>>> {
    let claims_obj = claims.as_object()?;
    let mut props: HashMap<String, Vec<String>> = HashMap::new();

    for (pid, claim_list) in claims_obj {
        let claim_array = claim_list.as_array()?;
        let mut values: Vec<String> = Vec::new();

        for claim in claim_array {
            let mainsnak = claim.get("mainsnak")?.as_object()?;
            let snaktype = mainsnak.get("snaktype")?.as_str()?;
            if snaktype != "value" {
                continue;
            }

            let datavalue = mainsnak.get("datavalue")?.as_object()?;
            let value = datavalue.get("value")?;

            if let Some(val) = extract_value(value) {
                values.push(val);
            }
        }

        // Keep the property key even when no concrete value is extractable.
        // This preserves presence-based indicators (somevalue/novalue).
        props.insert(pid.to_string(), values);
    }

    Some(props)
}

fn extract_value(value: &simd_json::OwnedValue) -> Option<String> {
    if let Some(obj) = value.as_object() {
        if let Some(id) = obj.get("id").and_then(|v| v.as_str()) {
            return Some(id.to_string());
        }
        if let Some(time) = obj.get("time").and_then(|v| v.as_str()) {
            // Simplify time format
            let t = time.trim_start_matches('+').trim_start_matches('-');
            let t = t.split('T').next().unwrap_or(t);
            return Some(t.to_string());
        }
        if let Some(amount) = obj.get("amount").and_then(|v| v.as_str()) {
            return Some(amount.trim_start_matches('+').to_string());
        }
        if let Some(text) = obj.get("text").and_then(|v| v.as_str()) {
            return Some(text.to_string());
        }
        let lat = obj
            .get("latitude")
            .and_then(|v| v.as_f64().or_else(|| v.as_i64().map(|x| x as f64)));
        let lon = obj
            .get("longitude")
            .and_then(|v| v.as_f64().or_else(|| v.as_i64().map(|x| x as f64)));
        if let Some(lat) = lat {
            if let Some(lon) = lon {
                return Some(format!("{:.6},{:.6}", lat, lon));
            }
        }
    }
    if let Some(s) = value.as_str() {
        return Some(s.to_string());
    }
    None
}

/// Resolve a Wikidata multilingual dict to a single string using language fallback.
fn resolve_lang(lang_obj: &simd_json::OwnedValue) -> Option<String> {
    const FALLBACK: &[&str] = &["en", "en-us", "en-gb", "mul"];
    let obj = lang_obj.as_object()?;
    for lang in FALLBACK {
        if let Some(entry) = obj.get(*lang) {
            return entry.as_object()?.get("value")?.as_str().map(String::from);
        }
    }
    // No accepted language found.
    None
}

/// Extract English aliases as a JSON array of strings.
fn extract_aliases(obj: &simd_json::owned::Object) -> Option<String> {
    let aliases = obj.get("aliases")?.as_object()?;
    let en = aliases.get("en")?.as_array()?;
    let vals: Vec<String> = en
        .iter()
        .filter_map(|v| v.as_object()?.get("value")?.as_str().map(String::from))
        .collect();
    if vals.is_empty() {
        None
    } else {
        let arr =
            serde_json::Value::Array(vals.into_iter().map(serde_json::Value::String).collect());
        serde_json::to_string(&arr).ok()
    }
}

/// Format a JSON number for coordinate output, matching Python's behavior:
/// - Integer JSON values (48) → "48"
/// - Float JSON values (48.0) → "48.0", (49.13835) → "49.13835"
fn format_json_number(v: &simd_json::OwnedValue) -> String {
    if let Some(i) = v.as_i64() {
        return i.to_string();
    }
    if let Some(f) = v.as_f64() {
        let s = format!("{:.6}", f);
        let trimmed = s.trim_end_matches('0');
        if trimmed.ends_with('.') {
            return format!("{}0", trimmed);
        }
        return trimmed.to_string();
    }
    "0".to_string()
}

/// Convert a Wikimedia Commons filename to a full URL.
fn commons_url(filename: &str) -> String {
    let mut url = String::with_capacity(55 + filename.len() * 3);
    url.push_str("https://commons.wikimedia.org/wiki/Special:FilePath/");
    for byte in filename.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'(' | b')' => {
                url.push(byte as char);
            }
            _ => {
                url.push_str(&format!("%{:02X}", byte));
            }
        }
    }
    url
}

/// Simplify a Wikidata datavalue (with "type" and "value" fields) into a string.
fn simplify_value(datavalue: &simd_json::OwnedValue) -> Option<String> {
    let obj = datavalue.as_object()?;
    let dtype = obj.get("type")?.as_str()?;
    let val = obj.get("value")?;

    match dtype {
        "wikibase-entityid" => val.as_object()?.get("id")?.as_str().map(String::from),
        "string" => val.as_str().map(String::from),
        "time" => {
            let val_obj = val.as_object()?;
            let mut t = val_obj.get("time")?.as_str()?.to_string();
            let precision = val_obj
                .get("precision")
                .and_then(|p| p.as_i64())
                .unwrap_or(11);
            if t.starts_with('+') {
                t = t[1..].to_string();
            }
            if let Some(idx) = t.find('T') {
                t = t[..idx].to_string();
            }
            if precision <= 9 {
                t = t.split('-').next().unwrap_or(&t).to_string();
            } else if precision == 10 {
                let parts: Vec<&str> = t.splitn(3, '-').collect();
                if parts.len() >= 2 {
                    t = format!("{}-{}", parts[0], parts[1]);
                }
            }
            Some(t)
        }
        "globecoordinate" => {
            let val_obj = val.as_object()?;
            let lat_str = format_json_number(val_obj.get("latitude")?);
            let lon_str = format_json_number(val_obj.get("longitude")?);
            Some(format!("{},{}", lat_str, lon_str))
        }
        "quantity" => {
            let val_obj = val.as_object()?;
            let amt_str = val_obj.get("amount")?.as_str()?;
            let amt = amt_str.strip_prefix('+').unwrap_or(amt_str);
            let unit = val_obj
                .get("unit")
                .and_then(|u| u.as_str())
                .unwrap_or("");
            if !unit.is_empty() && unit != "1" && unit.contains("entity/") {
                let unit_qid = unit.rsplit("entity/").next().unwrap_or("");
                Some(format!("{} {}", amt, unit_qid))
            } else {
                Some(amt.to_string())
            }
        }
        "monolingualtext" => val.as_object()?.get("text")?.as_str().map(String::from),
        _ => val.as_str().map(String::from),
    }
}

/// Simplify a snak into {"value": ..., "label": ...}.
fn simplify_snak(
    snak: &simd_json::OwnedValue,
    rtxn: &heed::RoTxn,
    db: &Database<Str, Str>,
    labels: &mut LabelCache,
) -> serde_json::Value {
    let obj = match snak.as_object() {
        Some(o) => o,
        None => return serde_json::json!({"value": null}),
    };

    let snaktype = obj
        .get("snaktype")
        .and_then(|v| v.as_str())
        .unwrap_or("value");

    if snaktype == "novalue" {
        return serde_json::json!({"label": "none", "value": null});
    }
    if snaktype == "somevalue" {
        return serde_json::json!({"label": "unknown", "value": null});
    }

    let datavalue = match obj.get("datavalue") {
        Some(dv) => dv,
        None => return serde_json::json!({"value": null}),
    };

    let mut value = simplify_value(datavalue);
    let mut result = serde_json::Map::new();

    // Convert commonsMedia filenames to full Wikimedia Commons URLs
    let snak_datatype = obj
        .get("datatype")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if snak_datatype == "commonsMedia" {
        if let Some(ref filename) = value {
            if !filename.is_empty() {
                value = Some(commons_url(filename));
            }
        }
    }

    let dtype = datavalue
        .as_object()
        .and_then(|o| o.get("type"))
        .and_then(|t| t.as_str())
        .unwrap_or("");

    if dtype == "wikibase-entityid" {
        if let Some(ref val) = value {
            if let Some(label) = labels.get(rtxn, db, val) {
                result.insert("label".into(), serde_json::Value::String(label));
            }
        }
    } else if dtype == "quantity" {
        if let Some(raw_val) = datavalue
            .as_object()
            .and_then(|o| o.get("value"))
            .and_then(|v| v.as_object())
        {
            let raw_unit = raw_val
                .get("unit")
                .and_then(|u| u.as_str())
                .unwrap_or("");
            if raw_unit.contains("entity/") {
                let unit_qid = raw_unit.rsplit("entity/").next().unwrap_or("");
                if let Some(unit_label) = labels.get(rtxn, db, unit_qid) {
                    let amt = raw_val
                        .get("amount")
                        .and_then(|a| a.as_str())
                        .unwrap_or("");
                    let amt = amt.strip_prefix('+').unwrap_or(amt);
                    result.insert(
                        "label".into(),
                        serde_json::Value::String(format!("{} {}", amt, unit_label)),
                    );
                }
            }
        }
    }

    result.insert(
        "value".into(),
        value
            .map(serde_json::Value::String)
            .unwrap_or(serde_json::Value::Null),
    );
    serde_json::Value::Object(result)
}

/// Simplify qualifiers into {"PID": {"label": ..., "values": [...]}}.
fn simplify_qualifiers(
    qualifiers: &simd_json::OwnedValue,
    rtxn: &heed::RoTxn,
    db: &Database<Str, Str>,
    labels: &mut LabelCache,
) -> serde_json::Value {
    let obj = match qualifiers.as_object() {
        Some(o) => o,
        None => return serde_json::Value::Object(serde_json::Map::new()),
    };

    let mut result = serde_json::Map::new();

    // Preserve Wikidata JSON key order (matches Python's dict iteration for sentences)
    for (pid, snaks) in obj {
        let snak_arr = match snaks.as_array() {
            Some(a) => a,
            None => continue,
        };
        let values: Vec<serde_json::Value> = snak_arr
            .iter()
            .map(|s| simplify_snak(s, rtxn, db, labels))
            .collect();
        let mut entry = serde_json::Map::new();
        if let Some(pid_label) = labels.get(rtxn, db, pid) {
            entry.insert("label".into(), serde_json::Value::String(pid_label));
        }
        entry.insert("values".into(), serde_json::Value::Array(values));
        result.insert(pid.clone(), serde_json::Value::Object(entry));
    }

    serde_json::Value::Object(result)
}

/// Generate a human-readable sentence from a simplified statement.
fn make_sentence(entity_label: &str, prop_label: &str, stmt_result: &serde_json::Value) -> String {
    // Match Python's truthiness: empty strings and null fall through to "unknown"
    let value_display = stmt_result
        .get("label")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            stmt_result
                .get("value")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
        })
        .unwrap_or("unknown");

    let mut sentence = format!("{}'s {} is {}", entity_label, prop_label, value_display);

    if let Some(qualifiers) = stmt_result.get("qualifiers").and_then(|q| q.as_object()) {
        let mut parts: Vec<String> = Vec::new();
        for (pid, q) in qualifiers {
            let q_label = q.get("label").and_then(|l| l.as_str()).unwrap_or(pid);
            let q_values: Vec<&str> = q
                .get("values")
                .and_then(|v| v.as_array())
                .map(|arr| {
                    arr.iter()
                        .map(|v| {
                            v.get("label")
                                .and_then(|x| x.as_str())
                                .filter(|s| !s.is_empty())
                                .or_else(|| {
                                    v.get("value")
                                        .and_then(|x| x.as_str())
                                        .filter(|s| !s.is_empty())
                                })
                                .unwrap_or("unknown")
                        })
                        .collect()
                })
                .unwrap_or_default();
            parts.push(format!("{}: {}", q_label, q_values.join(", ")));
        }
        if !parts.is_empty() {
            sentence.push_str(&format!(" ({})", parts.join("; ")));
        }
    }

    sentence.push('.');
    sentence
}

/// Simplify a statement into {"value", "rank", "label"?, "qualifiers"?, "sentence"?}.
fn simplify_statement(
    stmt: &simd_json::OwnedValue,
    rtxn: &heed::RoTxn,
    db: &Database<Str, Str>,
    labels: &mut LabelCache,
    entity_label: &str,
    prop_label: &str,
) -> serde_json::Value {
    let obj = match stmt.as_object() {
        Some(o) => o,
        None => return serde_json::Value::Null,
    };

    let mainsnak = obj.get("mainsnak").unwrap_or(stmt);
    let snak_result = simplify_snak(mainsnak, rtxn, db, labels);

    // Compute all values first, then assemble result
    let label_val = snak_result.get("label").cloned();
    let value_val = snak_result
        .get("value")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    let rank = obj
        .get("rank")
        .and_then(|r| r.as_str())
        .unwrap_or("normal");
    let qualifiers_val = obj
        .get("qualifiers")
        .map(|q| simplify_qualifiers(q, rtxn, db, labels));

    // Generate sentence (needs value + label + qualifiers available)
    let sentence_val = if !entity_label.is_empty() && !prop_label.is_empty() {
        let datatype = obj
            .get("mainsnak")
            .and_then(|ms| ms.as_object())
            .and_then(|ms| ms.get("datatype"))
            .and_then(|dt| dt.as_str())
            .unwrap_or("");

        if !matches!(
            datatype,
            "external-id" | "url" | "commonsMedia" | "geo-shape" | "tabular-data"
        ) {
            // Build temp object with all fields for sentence generation
            let mut temp = serde_json::Map::new();
            if let Some(ref label) = label_val {
                temp.insert("label".into(), label.clone());
            }
            if let Some(ref qual) = qualifiers_val {
                temp.insert("qualifiers".into(), qual.clone());
            }
            temp.insert("value".into(), value_val.clone());
            Some(make_sentence(
                entity_label,
                prop_label,
                &serde_json::Value::Object(temp),
            ))
        } else {
            None
        }
    } else {
        None
    };

    // Assemble result in alphabetical key order
    let mut result = serde_json::Map::new();
    if let Some(label) = label_val {
        result.insert("label".into(), label);
    }
    if let Some(qual) = qualifiers_val {
        result.insert("qualifiers".into(), qual);
    }
    result.insert("rank".into(), serde_json::Value::String(rank.to_string()));
    if let Some(sentence) = sentence_val {
        result.insert("sentence".into(), serde_json::Value::String(sentence));
    }
    result.insert("value".into(), value_val);

    serde_json::Value::Object(result)
}

/// Simplify all statements for a PID into {"label"?: ..., "statements": [...]}.
fn simplify_pid(
    pid: &str,
    stmts: &simd_json::OwnedValue,
    rtxn: &heed::RoTxn,
    db: &Database<Str, Str>,
    labels: &mut LabelCache,
    entity_label: &str,
) -> serde_json::Value {
    let pid_label = labels
        .get(rtxn, db, pid)
        .unwrap_or_else(|| pid.to_string());

    let stmts_arr = match stmts.as_array() {
        Some(a) => a,
        None => return serde_json::Value::Object(serde_json::Map::new()),
    };

    let statements: Vec<serde_json::Value> = stmts_arr
        .iter()
        .map(|s| simplify_statement(s, rtxn, db, labels, entity_label, &pid_label))
        .collect();

    let mut entry = serde_json::Map::new();
    if pid_label != pid {
        entry.insert("label".into(), serde_json::Value::String(pid_label));
    }
    entry.insert("statements".into(), serde_json::Value::Array(statements));
    serde_json::Value::Object(entry)
}

/// Organize raw claims into core/intrinsic/relational/unclaimed buckets with labels.
fn organize_claims(
    claims: &simd_json::OwnedValue,
    category: Option<&str>,
    modules: &[String],
    cat_pids_map: &HashMap<String, CategoryPids>,
    rtxn: &heed::RoTxn,
    db: &Database<Str, Str>,
    labels: &mut LabelCache,
    entity_label: &str,
) -> String {
    let claims_obj = match claims.as_object() {
        Some(o) => o,
        None => return "{}".to_string(),
    };

    let mut core = serde_json::Map::new();
    let mut intrinsic_map = serde_json::Map::new();
    let mut relational_map = serde_json::Map::new();
    let mut unclaimed = serde_json::Map::new();

    let category = match category {
        Some(c) if !c.is_empty() => c,
        _ => {
            // No category — everything is unclaimed
            let mut sorted_pids: Vec<&String> = claims_obj.keys().collect();
            sorted_pids.sort();
            for pid in sorted_pids {
                if let Some(stmts) = claims_obj.get(pid.as_str()) {
                    let entry = simplify_pid(pid, stmts, rtxn, db, labels, entity_label);
                    unclaimed.insert(pid.clone(), entry);
                }
            }
            let mut result = serde_json::Map::new();
            result.insert("core".into(), serde_json::Value::Object(serde_json::Map::new()));
            result.insert("intrinsic".into(), serde_json::Value::Object(serde_json::Map::new()));
            result.insert("relational".into(), serde_json::Value::Object(serde_json::Map::new()));
            result.insert("unclaimed".into(), serde_json::Value::Object(unclaimed));
            let sorted = sort_json_keys(&serde_json::Value::Object(result));
            return serde_json::to_string(&sorted)
                .unwrap_or_else(|_| "{}".to_string());
        }
    };

    let cat_pids = match cat_pids_map.get(category) {
        Some(cp) => cp,
        None => {
            let mut result = serde_json::Map::new();
            result.insert("core".into(), serde_json::Value::Object(serde_json::Map::new()));
            result.insert("intrinsic".into(), serde_json::Value::Object(serde_json::Map::new()));
            result.insert("relational".into(), serde_json::Value::Object(serde_json::Map::new()));
            result.insert("unclaimed".into(), serde_json::Value::Object(serde_json::Map::new()));
            return serde_json::to_string(&serde_json::Value::Object(result))
                .unwrap_or_else(|_| "{}".to_string());
        }
    };

    let module_pid_map = cat_pids.get_module_pids(modules);
    let mut claimed_pids: HashSet<String> = HashSet::new();

    // Core PIDs (sorted)
    let mut sorted_core: Vec<&String> = cat_pids.core_pids.iter().collect();
    sorted_core.sort();
    for pid in sorted_core {
        if let Some(stmts) = claims_obj.get(pid.as_str()) {
            claimed_pids.insert(pid.clone());
            let entry = simplify_pid(pid, stmts, rtxn, db, labels, entity_label);
            core.insert(pid.clone(), entry);
        }
    }

    // Module PIDs (sorted by pid, assigned to first module name alphabetically)
    // Route to intrinsic or relational bucket based on module type
    for (pid, mod_names) in &module_pid_map {
        if claimed_pids.contains(pid) {
            continue;
        }
        if let Some(stmts) = claims_obj.get(pid.as_str()) {
            claimed_pids.insert(pid.clone());
            let entry = simplify_pid(pid, stmts, rtxn, db, labels, entity_label);
            let mod_name = mod_names.iter().next().unwrap();
            let bucket = match cat_pids.module_types.get(mod_name).map(|s| s.as_str()) {
                Some("relational") => &mut relational_map,
                _ => &mut intrinsic_map,
            };
            let module_entry = bucket
                .entry(mod_name.clone())
                .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
            if let Some(obj) = module_entry.as_object_mut() {
                obj.insert(pid.clone(), entry);
            }
        }
    }

    // Unclaimed (remaining, sorted)
    let mut sorted_remaining: Vec<&String> = claims_obj
        .keys()
        .filter(|pid| !claimed_pids.contains(*pid))
        .collect();
    sorted_remaining.sort();
    for pid in sorted_remaining {
        if let Some(stmts) = claims_obj.get(pid.as_str()) {
            let entry = simplify_pid(pid, stmts, rtxn, db, labels, entity_label);
            unclaimed.insert(pid.clone(), entry);
        }
    }

    let mut result = serde_json::Map::new();
    result.insert("core".into(), serde_json::Value::Object(core));
    result.insert("intrinsic".into(), serde_json::Value::Object(intrinsic_map));
    result.insert("relational".into(), serde_json::Value::Object(relational_map));
    result.insert("unclaimed".into(), serde_json::Value::Object(unclaimed));
    let sorted = sort_json_keys(&serde_json::Value::Object(result));
    serde_json::to_string(&sorted).unwrap_or_else(|_| "{}".to_string())
}

/// Recursively sort JSON object keys (matches Python's json.dumps(sort_keys=True)).
fn sort_json_keys(value: &serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => {
            let mut sorted = serde_json::Map::new();
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for key in keys {
                if let Some(v) = map.get(key) {
                    sorted.insert(key.clone(), sort_json_keys(v));
                }
            }
            serde_json::Value::Object(sorted)
        }
        serde_json::Value::Array(arr) => {
            serde_json::Value::Array(arr.iter().map(sort_json_keys).collect())
        }
        _ => value.clone(),
    }
}

fn build_gate_qid_lookup(loader: &TagLoader) -> HashMap<String, String> {
    let mut lookup = HashMap::new();
    for cat_name in loader.get_category_names() {
        let qids = loader.get_gate_qids(&cat_name);
        for qid in qids {
            lookup.insert(qid, cat_name.clone());
        }
    }
    lookup
}

fn build_matched_indicators(
    props: &HashMap<String, Vec<String>>,
    loader: &TagLoader,
    category: &str,
    modules: &[String],
) -> BTreeMap<String, Vec<String>> {
    let mut result: BTreeMap<String, Vec<String>> = BTreeMap::new();

    let cat_config = match loader.categories.get(category) {
        Some(c) => c,
        None => return result,
    };

    for mod_name in modules {
        let mod_config = match cat_config.modules.get(mod_name) {
            Some(m) => m,
            None => continue,
        };

        let mut matched: Vec<String> = Vec::new();
        let mut indicator_pids: Vec<&String> = mod_config.indicators.keys().collect();
        indicator_pids.sort();

        for pid in indicator_pids {
            let Some(expected_values) = mod_config.indicators.get(pid) else {
                continue;
            };
            let entity_values = props.get(pid);

            match expected_values {
                None => {
                    // Presence-based
                    if props.contains_key(pid) {
                        matched.push(pid.clone());
                    }
                }
                Some(expected) => {
                    // Value-based: include matched values for parity with Python output
                    // (e.g., "P31:Q3305213"), sorted and deduplicated.
                    if let Some(entity_vals) = entity_values {
                        let expected_set: BTreeSet<&String> = expected.iter().collect();
                        let entity_set: BTreeSet<&String> = entity_vals.iter().collect();
                        for val in expected_set.intersection(&entity_set) {
                            matched.push(format!("{}:{}", pid, val));
                        }
                    }
                }
            }
        }

        if !matched.is_empty() {
            result.insert(mod_name.clone(), matched);
        }
    }

    result
}

fn create_parquet_schema() -> Arc<Schema> {
    Arc::new(Schema::new(vec![
        Field::new("qid", DataType::Utf8, false),
        Field::new("label", DataType::Utf8, false),
        Field::new("description", DataType::Utf8, true),
        Field::new("aliases", DataType::Utf8, true),
        Field::new(
            "p31_values",
            DataType::List(Arc::new(Field::new("item", DataType::Utf8, true))),
            false,
        ),
        Field::new("category", DataType::Utf8, true),
        Field::new(
            "intrinsic_modules",
            DataType::List(Arc::new(Field::new("item", DataType::Utf8, true))),
            false,
        ),
        Field::new(
            "relational_modules",
            DataType::List(Arc::new(Field::new("item", DataType::Utf8, true))),
            false,
        ),
        Field::new("gate_matched", DataType::Utf8, true),
        Field::new("gate_property", DataType::Utf8, true),
        Field::new("matched_indicators", DataType::Utf8, true),
        Field::new("claims", DataType::Utf8, false),
        Field::new("sitelinks", DataType::Utf8, true),
    ]))
}

fn write_batch(
    writer: &mut ArrowWriter<File>,
    schema: &Arc<Schema>,
    entities: &[ClassifiedEntity],
) -> Result<()> {
    let mut qid_builder = StringBuilder::new();
    let mut label_builder = StringBuilder::new();
    let mut description_builder = StringBuilder::new();
    let mut aliases_builder = StringBuilder::new();
    let mut p31_builder = ListBuilder::new(StringBuilder::new());
    let mut category_builder = StringBuilder::new();
    let mut intrinsic_builder = ListBuilder::new(StringBuilder::new());
    let mut relational_builder = ListBuilder::new(StringBuilder::new());
    let mut gate_builder = StringBuilder::new();
    let mut gate_property_builder = StringBuilder::new();
    let mut indicators_builder = StringBuilder::new();
    let mut claims_builder = StringBuilder::new();
    let mut sitelinks_builder = StringBuilder::new();

    for entity in entities {
        qid_builder.append_value(&entity.qid);
        label_builder.append_value(&entity.label);

        match &entity.description {
            Some(d) => description_builder.append_value(d),
            None => description_builder.append_null(),
        }

        match &entity.aliases {
            Some(a) => aliases_builder.append_value(a),
            None => aliases_builder.append_null(),
        }

        for val in &entity.p31_values {
            p31_builder.values().append_value(val);
        }
        p31_builder.append(true);

        match &entity.category {
            Some(c) => category_builder.append_value(c),
            None => category_builder.append_null(),
        }

        for val in &entity.intrinsic_modules {
            intrinsic_builder.values().append_value(val);
        }
        intrinsic_builder.append(true);

        for val in &entity.relational_modules {
            relational_builder.values().append_value(val);
        }
        relational_builder.append(true);

        match &entity.gate_matched {
            Some(g) => gate_builder.append_value(g),
            None => gate_builder.append_null(),
        }

        match &entity.gate_property {
            Some(gp) => gate_property_builder.append_value(gp),
            None => gate_property_builder.append_null(),
        }

        match &entity.matched_indicators {
            Some(i) => indicators_builder.append_value(i),
            None => indicators_builder.append_null(),
        }

        claims_builder.append_value(&entity.claims);

        match &entity.sitelinks {
            Some(s) => sitelinks_builder.append_value(s),
            None => sitelinks_builder.append_null(),
        }
    }

    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![
            Arc::new(qid_builder.finish()) as ArrayRef,
            Arc::new(label_builder.finish()) as ArrayRef,
            Arc::new(description_builder.finish()) as ArrayRef,
            Arc::new(aliases_builder.finish()) as ArrayRef,
            Arc::new(p31_builder.finish()) as ArrayRef,
            Arc::new(category_builder.finish()) as ArrayRef,
            Arc::new(intrinsic_builder.finish()) as ArrayRef,
            Arc::new(relational_builder.finish()) as ArrayRef,
            Arc::new(gate_builder.finish()) as ArrayRef,
            Arc::new(gate_property_builder.finish()) as ArrayRef,
            Arc::new(indicators_builder.finish()) as ArrayRef,
            Arc::new(claims_builder.finish()) as ArrayRef,
            Arc::new(sitelinks_builder.finish()) as ArrayRef,
        ],
    )?;

    writer.write(&batch)?;
    Ok(())
}

fn load_checkpoint(path: &PathBuf, restart: bool) -> Checkpoint {
    if restart {
        return Checkpoint::default();
    }

    match fs::read_to_string(path) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => Checkpoint::default(),
    }
}

fn save_checkpoint(path: &PathBuf, cp: &Checkpoint) -> Result<()> {
    let data = serde_json::to_string_pretty(cp)?;
    fs::write(path, data)?;
    Ok(())
}

fn extract_file_id(path: &PathBuf) -> String {
    let filename = path.file_name().unwrap_or_default().to_string_lossy();
    // Extract file number from pattern like wikidata-20260119-all_0001.jsonl.zst
    if let Some(idx) = filename.rfind('_') {
        let rest = &filename[idx + 1..];
        if let Some(dot_idx) = rest.find('.') {
            return rest[..dot_idx].to_string();
        }
    }
    filename.to_string()
}

/// Parse file ID as a number (e.g., "0005" -> 5)
fn parse_file_id_num(file_id: &str) -> Option<usize> {
    file_id.parse::<usize>().ok()
}

/// Filter files based on CLI arguments: --files, --start/--end, or --max-files
fn filter_files(files: Vec<PathBuf>, args: &Args) -> Vec<PathBuf> {
    // Priority 1: Explicit file list
    if let Some(ref file_ids) = args.files {
        let file_id_set: std::collections::HashSet<&str> =
            file_ids.iter().map(|s| s.as_str()).collect();
        let filtered: Vec<PathBuf> = files
            .into_iter()
            .filter(|f| file_id_set.contains(extract_file_id(f).as_str()))
            .collect();
        println!("Filtering by --files: {} files selected", filtered.len());
        return filtered;
    }

    // Priority 2: Range-based filtering
    if args.start.is_some() || args.end.is_some() {
        let start = args.start.unwrap_or(0);
        let end = args.end.unwrap_or(usize::MAX);
        let filtered: Vec<PathBuf> = files
            .into_iter()
            .filter(|f| {
                let file_id = extract_file_id(f);
                if let Some(num) = parse_file_id_num(&file_id) {
                    num >= start && num <= end
                } else {
                    false
                }
            })
            .collect();
        println!(
            "Filtering by --start {} --end {}: {} files selected",
            start,
            args.end.map_or("∞".to_string(), |e| e.to_string()),
            filtered.len()
        );
        return filtered;
    }

    // Priority 3: max_files (backwards compatibility)
    if args.max_files > 0 && args.max_files < files.len() {
        let mut truncated = files;
        truncated.truncate(args.max_files);
        println!(
            "Filtering by --max-files {}: {} files selected",
            args.max_files,
            truncated.len()
        );
        return truncated;
    }

    files
}

fn human_bytes(bytes: u64) -> String {
    const UNITS: &[&str] = &["B", "KB", "MB", "GB", "TB"];
    let mut size = bytes as f64;
    let mut unit_idx = 0;

    while size >= 1024.0 && unit_idx < UNITS.len() - 1 {
        size /= 1024.0;
        unit_idx += 1;
    }

    format!("{:.1}{}", size, UNITS[unit_idx])
}

impl Clone for FileStats {
    fn clone(&self) -> Self {
        FileStats {
            total_records: self.total_records,
            items: self.items,
            with_label: self.with_label,
            classified: self.classified,
            with_modules: self.with_modules,
            unclassified: self.unclassified,
        }
    }
}
