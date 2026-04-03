//! Filter Wikidata dump files to keep only core-tier entities.
//! Uses src/bulk_import.yaml detection rules (standalone, no TagLoader).
//! Processes multiple dump files in parallel (file-level parallelism).
//!
//! Input:  data_dump/wikidata-20260119-all_0001.jsonl.zst
//! Output: data_dump_core/wikidata-20260119-all_core_0001.jsonl.zst

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::Result;
use clap::Parser;
use heed::types::Str;
use heed::{Database, EnvOpenOptions};
use indicatif::{MultiProgress, ProgressBar, ProgressStyle};
use parking_lot::Mutex;
use serde::Deserialize;
use simd_json::prelude::*;

// ============================================================
// CLI Args
// ============================================================

#[derive(Parser, Debug)]
#[command(name = "filter_core")]
#[command(about = "Filter Wikidata dump to keep only core-tier entities")]
struct Args {
    /// Input directory containing .jsonl.zst dump files
    #[arg(long, default_value = "data_dump")]
    input_dir: PathBuf,

    /// Output directory for filtered core files
    #[arg(long, default_value = "data_dump_core")]
    output_dir: PathBuf,

    /// Bulk-import YAML config
    #[arg(long, default_value = "src/bulk_import.yaml")]
    config: PathBuf,

    /// LMDB database for label lookup
    #[arg(long, default_value = "data_dump_output/labels_en.lmdb")]
    lmdb: PathBuf,

    /// Number of files to process in parallel
    #[arg(long, default_value = "12")]
    workers: usize,
}

// ============================================================
// YAML config types (same as detect_bulk.rs)
// ============================================================

#[derive(Debug, Deserialize)]
struct BulkConfig {
    source_databases: HashMap<String, SourceDatabase>,
    human_curation_indicators: CurationIndicators,
    detection_rules: DetectionRules,
    #[allow(dead_code)]
    people_quality: PeopleQuality,
}

#[derive(Debug, Deserialize)]
struct SourceDatabase {
    #[allow(dead_code)]
    description: String,
    properties: Vec<String>,
    signature_props: Vec<String>,
    #[serde(default)]
    signature_value_props: HashMap<String, Vec<String>>,
}

#[derive(Debug, Deserialize)]
struct CurationIndicators {
    strong: Vec<String>,
    medium: Vec<String>,
    weak: Vec<String>,
    people_strong: Vec<String>,
    people_weak: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct DetectionRules {
    bulk_ratio_threshold: f64,
    curation_score_protect: u32,
    #[serde(default)]
    structural_p31_qids: Vec<String>,
    #[serde(default)]
    extended_p31_qids: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct PeopleQuality {
    #[allow(dead_code)]
    core_biographical_props: Vec<String>,
    #[allow(dead_code)]
    min_core_props_for_low: usize,
}

// ============================================================
// Precomputed lookup tables
// ============================================================

struct BulkLookup {
    signature_props: HashMap<String, HashSet<String>>,
    /// source_database name -> map of PID -> set of required QID values
    signature_value_props: HashMap<String, HashMap<String, HashSet<String>>>,
    /// Set of all PIDs that need value extraction (for optimization)
    value_extraction_pids: HashSet<String>,
    source_all_props: HashMap<String, HashSet<String>>,
    all_bulk_props: HashSet<String>,
    strong_props: HashSet<String>,
    medium_props: HashSet<String>,
    weak_props: HashSet<String>,
    people_strong_props: HashSet<String>,
    people_weak_props: HashSet<String>,
    bulk_ratio_threshold: f64,
    curation_score_protect: u32,
    structural_p31_qids: HashSet<String>,
    extended_p31_qids: HashSet<String>,
}

impl BulkLookup {
    fn from_config(config: &BulkConfig) -> Self {
        let mut signature_props: HashMap<String, HashSet<String>> = HashMap::new();
        let mut signature_value_props: HashMap<String, HashMap<String, HashSet<String>>> =
            HashMap::new();
        let mut value_extraction_pids: HashSet<String> = HashSet::new();
        let mut source_all_props: HashMap<String, HashSet<String>> = HashMap::new();
        let mut all_bulk_props: HashSet<String> = HashSet::new();

        for (name, db) in &config.source_databases {
            let sigs: HashSet<String> = db.signature_props.iter().cloned().collect();
            signature_props.insert(name.clone(), sigs);

            // Build value-based signature lookup
            let mut val_sigs: HashMap<String, HashSet<String>> = HashMap::new();
            for (pid, qids) in &db.signature_value_props {
                value_extraction_pids.insert(pid.clone());
                val_sigs.insert(pid.clone(), qids.iter().cloned().collect());
            }
            signature_value_props.insert(name.clone(), val_sigs);

            let mut all_for_source: HashSet<String> = HashSet::new();
            for pid in &db.properties {
                all_bulk_props.insert(pid.clone());
                all_for_source.insert(pid.clone());
            }
            source_all_props.insert(name.clone(), all_for_source);
        }

        BulkLookup {
            signature_props,
            signature_value_props,
            value_extraction_pids,
            source_all_props,
            all_bulk_props,
            strong_props: config
                .human_curation_indicators
                .strong
                .iter()
                .cloned()
                .collect(),
            medium_props: config
                .human_curation_indicators
                .medium
                .iter()
                .cloned()
                .collect(),
            weak_props: config
                .human_curation_indicators
                .weak
                .iter()
                .cloned()
                .collect(),
            people_strong_props: config
                .human_curation_indicators
                .people_strong
                .iter()
                .cloned()
                .collect(),
            people_weak_props: config
                .human_curation_indicators
                .people_weak
                .iter()
                .cloned()
                .collect(),
            bulk_ratio_threshold: config.detection_rules.bulk_ratio_threshold,
            curation_score_protect: config.detection_rules.curation_score_protect,
            structural_p31_qids: config
                .detection_rules
                .structural_p31_qids
                .iter()
                .cloned()
                .collect(),
            extended_p31_qids: config
                .detection_rules
                .extended_p31_qids
                .iter()
                .cloned()
                .collect(),
        }
    }
}

// ============================================================
// Tier enum
// ============================================================

#[derive(Debug, Clone, Copy, PartialEq)]
enum Tier {
    Core,
    Extended,
    Structural,
}

// ============================================================
// Per-file stats
// ============================================================

#[derive(Debug)]
struct FileStats {
    filename: String,
    total_lines: u64,
    items_with_label: u64,
    core: u64,
    extended: u64,
    structural: u64,
}

// ============================================================
// Main
// ============================================================

fn main() -> Result<()> {
    let args = Args::parse();

    // Load config
    let config_str = std::fs::read_to_string(&args.config)?;
    let config: BulkConfig = serde_yaml::from_str(&config_str)?;
    let lookup = Arc::new(BulkLookup::from_config(&config));

    eprintln!(
        "Loaded bulk_import.yaml: {} source databases, {} structural P31 QIDs",
        config.source_databases.len(),
        config.detection_rules.structural_p31_qids.len(),
    );

    // Open LMDB
    let lmdb_env = unsafe {
        EnvOpenOptions::new()
            .max_dbs(1)
            .map_size(20 * 1024 * 1024 * 1024)
            .open(&args.lmdb)?
    };
    let lmdb_env = Arc::new(lmdb_env);

    // Verify LMDB
    {
        let rtxn = lmdb_env.read_txn()?;
        let _db: Database<Str, Str> = lmdb_env
            .open_database(&rtxn, Some("labels_en"))?
            .expect("labels_en database not found");
        drop(rtxn);
    }

    // Create output directory
    fs::create_dir_all(&args.output_dir)?;

    // Find dump files
    let mut files: Vec<PathBuf> = fs::read_dir(&args.input_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.to_str().map_or(false, |s| s.ends_with(".jsonl.zst")))
        .collect();
    files.sort();

    if files.is_empty() {
        eprintln!("No .jsonl.zst files found in {:?}", args.input_dir);
        return Ok(());
    }

    eprintln!(
        "Found {} dump files, processing {} at a time",
        files.len(),
        args.workers
    );

    // Process files in parallel
    let multi_progress = MultiProgress::new();
    let all_stats: Arc<Mutex<Vec<FileStats>>> = Arc::new(Mutex::new(Vec::new()));

    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(args.workers)
        .build()?;

    pool.scope(|s| {
        for file in &files {
            let lookup = lookup.clone();
            let lmdb_env = lmdb_env.clone();
            let output_dir = args.output_dir.clone();
            let all_stats = all_stats.clone();
            let mp = &multi_progress;

            s.spawn(move |_| {
                let fname = file.file_name().unwrap().to_string_lossy().to_string();
                let output_path = make_output_path(file, &output_dir);
                let pb = mp.add(ProgressBar::new_spinner());
                pb.set_style(
                    ProgressStyle::default_spinner()
                        .template("{spinner:.green} {prefix}: {msg}")
                        .unwrap(),
                );
                pb.set_prefix(fname.clone());
                pb.enable_steady_tick(std::time::Duration::from_millis(200));

                match process_file(file, &output_path, &lmdb_env, &lookup, &pb) {
                    Ok(stats) => {
                        let pct = if stats.items_with_label > 0 {
                            stats.core as f64 / stats.items_with_label as f64 * 100.0
                        } else {
                            0.0
                        };
                        pb.finish_with_message(format!(
                            "done: {}/{} core ({:.1}%)",
                            stats.core, stats.items_with_label, pct
                        ));
                        all_stats.lock().push(stats);
                    }
                    Err(e) => {
                        pb.finish_with_message(format!("ERROR: {}", e));
                    }
                }
            });
        }
    });

    // Print summary table
    let mut stats_vec = Arc::try_unwrap(all_stats).unwrap().into_inner();
    stats_vec.sort_by(|a, b| a.filename.cmp(&b.filename));

    eprintln!();
    eprintln!(
        "{:<50} {:>10} {:>10} {:>10} {:>10} {:>7}",
        "File", "Labeled", "Core", "Extended", "Struct", "Core%"
    );
    eprintln!("{}", "-".repeat(100));

    let mut total_labeled = 0u64;
    let mut total_core = 0u64;
    let mut total_ext = 0u64;
    let mut total_struct = 0u64;

    for s in &stats_vec {
        let pct = if s.items_with_label > 0 {
            s.core as f64 / s.items_with_label as f64 * 100.0
        } else {
            0.0
        };
        eprintln!(
            "{:<50} {:>10} {:>10} {:>10} {:>10} {:>6.1}%",
            s.filename, s.items_with_label, s.core, s.extended, s.structural, pct
        );
        total_labeled += s.items_with_label;
        total_core += s.core;
        total_ext += s.extended;
        total_struct += s.structural;
    }

    eprintln!("{}", "-".repeat(100));
    let total_pct = if total_labeled > 0 {
        total_core as f64 / total_labeled as f64 * 100.0
    } else {
        0.0
    };
    eprintln!(
        "{:<50} {:>10} {:>10} {:>10} {:>10} {:>6.1}%",
        "TOTAL", total_labeled, total_core, total_ext, total_struct, total_pct
    );

    Ok(())
}

// ============================================================
// File path helpers
// ============================================================

fn make_output_path(input: &PathBuf, output_dir: &PathBuf) -> PathBuf {
    let name = input.file_name().unwrap().to_string_lossy();
    // wikidata-20260119-all_0001.jsonl.zst -> wikidata-20260119-all_core_0001.jsonl.zst
    let new_name = name.replace("all_", "all_core_");
    output_dir.join(&*new_name)
}

// ============================================================
// Per-file processing
// ============================================================

fn process_file(
    input: &PathBuf,
    output: &PathBuf,
    lmdb_env: &heed::Env,
    lookup: &BulkLookup,
    pb: &ProgressBar,
) -> Result<FileStats> {
    let fname = input.file_name().unwrap().to_string_lossy().to_string();

    // Open LMDB read transaction
    let rtxn = lmdb_env.read_txn()?;
    let db: Database<Str, Str> = lmdb_env
        .open_database(&rtxn, Some("labels_en"))?
        .expect("labels_en database not found");

    // Open input (zstd decompression)
    let in_file = File::open(input)?;
    let decoder = zstd::Decoder::new(in_file)?;
    let reader = BufReader::with_capacity(1024 * 1024, decoder);

    // Open output (zstd compression)
    let out_file = File::create(output)?;
    let buf_writer = BufWriter::with_capacity(1024 * 1024, out_file);
    let mut encoder = zstd::Encoder::new(buf_writer, 3)?;

    let mut stats = FileStats {
        filename: fname,
        total_lines: 0,
        items_with_label: 0,
        core: 0,
        extended: 0,
        structural: 0,
    };

    for line_result in reader.lines() {
        let line = line_result?;
        stats.total_lines += 1;

        if stats.total_lines % 100_000 == 0 {
            pb.set_message(format!("{} lines, {} core", stats.total_lines, stats.core));
        }

        match classify_entity(&line, &rtxn, &db, lookup) {
            Some(Tier::Core) => {
                stats.items_with_label += 1;
                stats.core += 1;
                writeln!(encoder, "{}", line)?;
            }
            Some(Tier::Extended) => {
                stats.items_with_label += 1;
                stats.extended += 1;
            }
            Some(Tier::Structural) => {
                stats.items_with_label += 1;
                stats.structural += 1;
            }
            None => {} // not an item or no English label
        }
    }

    // Finalize zstd frame
    let mut buf = encoder.finish()?;
    buf.flush()?;

    Ok(stats)
}

// ============================================================
// Entity classification (detection logic from detect_bulk.rs)
// ============================================================

fn classify_entity(
    line: &str,
    rtxn: &heed::RoTxn,
    lmdb_db: &Database<Str, Str>,
    lookup: &BulkLookup,
) -> Option<Tier> {
    let mut line_bytes = line.as_bytes().to_vec();
    let value: simd_json::OwnedValue = simd_json::to_owned_value(&mut line_bytes).ok()?;
    let obj = value.as_object()?;

    // Only items
    let entity_type = obj.get("type")?.as_str()?;
    if entity_type != "item" {
        return None;
    }

    let qid = obj.get("id")?.as_str()?;
    if qid.is_empty() {
        return None;
    }

    // English label from LMDB
    let _label = lmdb_db.get(rtxn, qid).ok().flatten()?;

    // Extract property keys and P31 values
    let claims = obj.get("claims")?;
    let claims_obj = claims.as_object()?;

    let mut property_keys: HashSet<String> = HashSet::new();
    let mut p31_values: Vec<String> = Vec::new();
    let mut property_values: HashMap<String, Vec<String>> = HashMap::new();

    for (pid, claim_list) in claims_obj {
        property_keys.insert(pid.to_string());

        // Extract QID values for P31 and value-based signature PIDs
        let need_values = pid == "P31" || lookup.value_extraction_pids.contains(pid);
        if need_values {
            if let Some(arr) = claim_list.as_array() {
                for claim in arr {
                    if let Some(qid_val) = extract_p31_value(claim) {
                        if pid == "P31" {
                            p31_values.push(qid_val.clone());
                        }
                        property_values
                            .entry(pid.to_string())
                            .or_default()
                            .push(qid_val);
                    }
                }
            }
        }
    }

    let is_person = p31_values.iter().any(|v| v == "Q5");

    // Rule 1a: P31-based structural detection
    if p31_values
        .iter()
        .any(|v| lookup.structural_p31_qids.contains(v))
    {
        return Some(Tier::Structural);
    }

    // Rule 1b: P31-based extended detection (bulk-imported domain content)
    if p31_values
        .iter()
        .any(|v| lookup.extended_p31_qids.contains(v))
    {
        return Some(Tier::Extended);
    }

    // Compute bulk ratio (exclude P31 from ratio)
    let props_for_ratio: Vec<&String> = property_keys
        .iter()
        .filter(|p| p.as_str() != "P31")
        .collect();
    let total_props = props_for_ratio.len();

    if total_props == 0 {
        return Some(Tier::Core);
    }

    let mut bulk_prop_count = 0;
    for pid in &props_for_ratio {
        if lookup.all_bulk_props.contains(pid.as_str()) {
            bulk_prop_count += 1;
        }
    }
    let bulk_ratio = bulk_prop_count as f64 / total_props as f64;

    // Signature-based source database matching
    let mut has_signature_match = false;
    for (src_name, sig_set) in &lookup.signature_props {
        let presence_sig_count = property_keys
            .iter()
            .filter(|p| p.as_str() != "P31" && sig_set.contains(p.as_str()))
            .count();

        // Count value-based signature matches
        let value_sig_count = if let Some(val_sigs) = lookup.signature_value_props.get(src_name) {
            val_sigs
                .iter()
                .filter(|(pid, required_qids)| {
                    if let Some(actual_vals) = property_values.get(pid.as_str()) {
                        actual_vals.iter().any(|v| required_qids.contains(v))
                    } else {
                        false
                    }
                })
                .count()
        } else {
            0
        };

        let sig_count = presence_sig_count + value_sig_count;

        if sig_count >= 2 {
            has_signature_match = true;
            break;
        } else if sig_count == 1 {
            if let Some(all_props) = lookup.source_all_props.get(src_name) {
                let non_sig_count = property_keys
                    .iter()
                    .filter(|p| {
                        p.as_str() != "P31"
                            && all_props.contains(p.as_str())
                            && !sig_set.contains(p.as_str())
                    })
                    .count();
                if non_sig_count >= 3 {
                    has_signature_match = true;
                    break;
                }
            }
        }
    }

    // Compute curation score
    let mut curation_score: u32 = 0;
    for pid in &property_keys {
        if lookup.strong_props.contains(pid) {
            curation_score += 3;
        }
        if lookup.medium_props.contains(pid) {
            curation_score += 2;
        }
        if lookup.weak_props.contains(pid) {
            curation_score += 1;
        }
        if is_person && lookup.people_strong_props.contains(pid) {
            curation_score += 2;
        }
        if is_person && lookup.people_weak_props.contains(pid) {
            curation_score += 1;
        }
    }

    // Rule 2: Signature-based detection
    if has_signature_match && curation_score <= 1 {
        return Some(Tier::Extended);
    }

    // Rule 3: Ratio-based detection
    if bulk_ratio >= lookup.bulk_ratio_threshold && curation_score < lookup.curation_score_protect {
        return Some(Tier::Extended);
    }

    // Default: core
    Some(Tier::Core)
}

fn extract_p31_value(claim: &simd_json::OwnedValue) -> Option<String> {
    let mainsnak = claim.get("mainsnak")?.as_object()?;
    let snaktype = mainsnak.get("snaktype")?.as_str()?;
    if snaktype != "value" {
        return None;
    }
    let datavalue = mainsnak.get("datavalue")?.as_object()?;
    let value = datavalue.get("value")?.as_object()?;
    let id = value.get("id")?.as_str()?;
    Some(id.to_string())
}
