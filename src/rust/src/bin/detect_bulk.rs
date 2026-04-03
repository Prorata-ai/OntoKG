//! Detect bulk-imported Wikidata entities using src/bulk_import.yaml.
//! Standalone binary -- does NOT use tags::TagLoader or src/tags/.

use std::collections::{HashMap, HashSet};
use std::fmt::Write as FmtWrite;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use anyhow::Result;
use clap::Parser;
use crossbeam_channel::{bounded, Receiver, Sender};
use heed::types::Str;
use heed::{Database, EnvOpenOptions};
use indicatif::{ProgressBar, ProgressStyle};
use parking_lot::Mutex;
use serde::Deserialize;
use simd_json::prelude::*;

// ============================================================
// CLI Args
// ============================================================

#[derive(Parser, Debug)]
#[command(name = "detect_bulk")]
#[command(about = "Detect bulk-imported Wikidata entities")]
struct Args {
    /// Single dump file to process (.jsonl.zst)
    #[arg(long)]
    file: PathBuf,

    /// Bulk-import YAML config
    #[arg(long, default_value = "src/bulk_import.yaml")]
    config: PathBuf,

    /// LMDB database for label lookup
    #[arg(long, default_value = "data_dump_output/labels_en.lmdb")]
    lmdb: PathBuf,

    /// Number of worker threads
    #[arg(long, default_value = "8")]
    workers: usize,

    /// Output report file (default: stdout)
    #[arg(long)]
    output: Option<PathBuf>,
}

// ============================================================
// YAML config types
// ============================================================

#[derive(Debug, Deserialize)]
struct BulkConfig {
    source_databases: HashMap<String, SourceDatabase>,
    human_curation_indicators: CurationIndicators,
    detection_rules: DetectionRules,
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
}

#[derive(Debug, Deserialize)]
struct PeopleQuality {
    core_biographical_props: Vec<String>,
    min_core_props_for_low: usize,
}

// ============================================================
// Precomputed lookup tables (built once, shared across threads)
// ============================================================

struct BulkLookup {
    /// PID -> list of source_database names that include it
    prop_to_sources: HashMap<String, Vec<String>>,
    /// source_database name -> set of signature PIDs
    signature_props: HashMap<String, HashSet<String>>,
    /// source_database name -> map of PID -> set of required QID values
    signature_value_props: HashMap<String, HashMap<String, HashSet<String>>>,
    /// Set of all PIDs that need value extraction (for optimization)
    value_extraction_pids: HashSet<String>,
    /// source_database name -> set of all PIDs (for counting non-signature hits)
    source_all_props: HashMap<String, HashSet<String>>,
    /// All source database property PIDs (flat set for fast lookup)
    all_bulk_props: HashSet<String>,
    /// Curation indicator sets
    strong_props: HashSet<String>,
    medium_props: HashSet<String>,
    weak_props: HashSet<String>,
    people_strong_props: HashSet<String>,
    people_weak_props: HashSet<String>,
    /// People quality
    core_bio_props: HashSet<String>,
    min_core_for_low: usize,
    /// Thresholds
    bulk_ratio_threshold: f64,
    curation_score_protect: u32,
    /// P31 QIDs that force structural tier
    structural_p31_qids: HashSet<String>,
}

impl BulkLookup {
    fn from_config(config: &BulkConfig) -> Self {
        let mut prop_to_sources: HashMap<String, Vec<String>> = HashMap::new();
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
                prop_to_sources
                    .entry(pid.clone())
                    .or_default()
                    .push(name.clone());
                all_bulk_props.insert(pid.clone());
                all_for_source.insert(pid.clone());
            }
            source_all_props.insert(name.clone(), all_for_source);
        }

        BulkLookup {
            prop_to_sources,
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
            core_bio_props: config
                .people_quality
                .core_biographical_props
                .iter()
                .cloned()
                .collect(),
            min_core_for_low: config.people_quality.min_core_props_for_low,
            bulk_ratio_threshold: config.detection_rules.bulk_ratio_threshold,
            curation_score_protect: config.detection_rules.curation_score_protect,
            structural_p31_qids: config
                .detection_rules
                .structural_p31_qids
                .iter()
                .cloned()
                .collect(),
        }
    }
}

// ============================================================
// Per-entity result
// ============================================================

#[derive(Debug, Clone)]
struct EntityResult {
    qid: String,
    label: String,
    tier: String,        // core, extended, structural
    tier_reason: String, // why this tier was assigned
    bulk_ratio: f64,
    curation_score: u32,
    total_props: usize,
    bulk_prop_count: usize,
    matched_sources: Vec<String>,
    is_person: bool,
    people_quality_tier: Option<String>,
    #[allow(dead_code)]
    p31_values: Vec<String>,
}

// ============================================================
// Aggregated statistics
// ============================================================

#[derive(Debug, Default)]
struct Stats {
    total_lines: u64,
    total_items: u64,
    with_label: u64,
    tier_core: u64,
    tier_extended: u64,
    tier_structural: u64,
    /// source_db_name -> count of entities with at least one hit
    source_hits: HashMap<String, u64>,
    /// bulk_ratio bucket (0..10 for 0.0-0.1, 0.1-0.2, ..., 0.9-1.0)
    bulk_ratio_histogram: [u64; 10],
    /// curation_score bucket: 0, 1, 2, ..., 10+
    curation_score_histogram: [u64; 12],
    /// People quality tiers
    people_quality_high: u64,
    people_quality_medium: u64,
    people_quality_low: u64,
    people_quality_stub: u64,
    people_total: u64,
    /// Samples near threshold (bulk_ratio 0.60-0.80)
    near_threshold_samples: Vec<EntityResult>,
    /// Potential false positives (flagged bulk but high curation)
    false_positive_samples: Vec<EntityResult>,
    /// Sample core entities (to verify they are truly human-curated)
    core_samples: Vec<EntityResult>,
    /// Sample extended entities
    extended_samples: Vec<EntityResult>,
    /// Sample structural entities
    structural_samples: Vec<EntityResult>,
    /// Signature detection counts per source database
    signature_detections: HashMap<String, u64>,
    /// Entities classified by each rule
    rule_structural_p31: u64,
    rule_signature: u64,
    rule_ratio: u64,
    rule_core: u64,
}

impl Clone for Stats {
    fn clone(&self) -> Self {
        Stats {
            total_lines: self.total_lines,
            total_items: self.total_items,
            with_label: self.with_label,
            tier_core: self.tier_core,
            tier_extended: self.tier_extended,
            tier_structural: self.tier_structural,
            source_hits: self.source_hits.clone(),
            bulk_ratio_histogram: self.bulk_ratio_histogram,
            curation_score_histogram: self.curation_score_histogram,
            people_quality_high: self.people_quality_high,
            people_quality_medium: self.people_quality_medium,
            people_quality_low: self.people_quality_low,
            people_quality_stub: self.people_quality_stub,
            people_total: self.people_total,
            near_threshold_samples: self.near_threshold_samples.clone(),
            false_positive_samples: self.false_positive_samples.clone(),
            core_samples: self.core_samples.clone(),
            extended_samples: self.extended_samples.clone(),
            structural_samples: self.structural_samples.clone(),
            signature_detections: self.signature_detections.clone(),
            rule_structural_p31: self.rule_structural_p31,
            rule_signature: self.rule_signature,
            rule_ratio: self.rule_ratio,
            rule_core: self.rule_core,
        }
    }
}

const MAX_SAMPLES: usize = 20;

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
        "Loaded bulk_import.yaml: {} source databases, {} curation indicators",
        config.source_databases.len(),
        config.human_curation_indicators.strong.len()
            + config.human_curation_indicators.medium.len()
            + config.human_curation_indicators.weak.len()
            + config.human_curation_indicators.people_strong.len()
            + config.human_curation_indicators.people_weak.len(),
    );

    // Open LMDB
    let lmdb_env = unsafe {
        EnvOpenOptions::new()
            .max_dbs(1)
            .map_size(20 * 1024 * 1024 * 1024) // 20GB
            .open(&args.lmdb)?
    };
    let lmdb_env = Arc::new(lmdb_env);

    // Verify we can read the DB
    {
        let rtxn = lmdb_env.read_txn()?;
        let _db: Database<Str, Str> = lmdb_env
            .open_database(&rtxn, Some("labels_en"))?
            .expect("labels_en database not found");
        drop(rtxn);
    }

    // Open dump file
    let file = File::open(&args.file)?;
    let file_size = file.metadata()?.len();
    let decoder = zstd::Decoder::new(file)?;
    let reader = BufReader::with_capacity(1024 * 1024, decoder);

    eprintln!(
        "Processing: {:?} ({:.1} MB compressed)",
        args.file,
        file_size as f64 / 1048576.0
    );

    // Progress bar
    let progress = ProgressBar::new(file_size);
    progress.set_style(
        ProgressStyle::default_bar()
            .template("[{elapsed_precise}] [{bar:40.cyan/blue}] {bytes}/{total_bytes} ({bytes_per_sec}, ETA {eta})")?
    );

    // Channels
    let worker_count = args.workers;
    let (line_tx, line_rx): (Sender<String>, Receiver<String>) = bounded(worker_count * 100);
    let (result_tx, result_rx): (Sender<EntityResult>, Receiver<EntityResult>) =
        bounded(worker_count * 100);

    let line_count = Arc::new(AtomicU64::new(0));

    // Reader thread
    let line_count_r = line_count.clone();
    let reader_handle = std::thread::spawn(move || {
        for line in reader.lines() {
            match line {
                Ok(line) => {
                    line_count_r.fetch_add(1, Ordering::Relaxed);
                    if line_tx.send(line).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        drop(line_tx);
    });

    // Worker threads
    let worker_handles: Vec<_> = (0..worker_count)
        .map(|_| {
            let line_rx = line_rx.clone();
            let result_tx = result_tx.clone();
            let lmdb_env = lmdb_env.clone();
            let lookup = lookup.clone();

            std::thread::spawn(move || {
                let rtxn = match lmdb_env.read_txn() {
                    Ok(txn) => txn,
                    Err(e) => {
                        eprintln!("Failed to create read txn: {:?}", e);
                        return;
                    }
                };
                let db: Database<Str, Str> = match lmdb_env.open_database(&rtxn, Some("labels_en"))
                {
                    Ok(Some(db)) => db,
                    _ => {
                        eprintln!("Failed to open db in worker");
                        return;
                    }
                };

                for line in line_rx {
                    if let Some(result) = process_entity(&line, &rtxn, &db, &lookup) {
                        if result_tx.send(result).is_err() {
                            break;
                        }
                    }
                }
            })
        })
        .collect();

    drop(line_rx);
    drop(result_tx);

    // Collector thread: aggregate stats
    let stats = Arc::new(Mutex::new(Stats::default()));
    let stats_c = stats.clone();
    let collector_handle = std::thread::spawn(move || {
        for result in result_rx {
            let mut s = stats_c.lock();
            s.with_label += 1;

            // Tier counts
            match result.tier.as_str() {
                "core" => s.tier_core += 1,
                "extended" => s.tier_extended += 1,
                "structural" => s.tier_structural += 1,
                _ => {}
            }

            // Track which rule classified this entity
            match result.tier_reason.as_str() {
                "structural_p31" => s.rule_structural_p31 += 1,
                "signature" => {
                    s.rule_signature += 1;
                    // Track which source's signature triggered
                    // (encoded as "signature:source_name" but we just count overall)
                }
                "ratio" => s.rule_ratio += 1,
                "core" => s.rule_core += 1,
                _ => {
                    // signature:source_name format
                    if result.tier_reason.starts_with("signature:") {
                        s.rule_signature += 1;
                        let src = &result.tier_reason["signature:".len()..];
                        *s.signature_detections.entry(src.to_string()).or_default() += 1;
                    }
                }
            }

            // Source hits
            for src in &result.matched_sources {
                *s.source_hits.entry(src.clone()).or_default() += 1;
            }

            // Bulk ratio histogram
            let bucket = (result.bulk_ratio * 10.0).floor() as usize;
            let bucket = bucket.min(9);
            s.bulk_ratio_histogram[bucket] += 1;

            // Curation score histogram
            let cs_bucket = (result.curation_score as usize).min(11);
            s.curation_score_histogram[cs_bucket] += 1;

            // People quality
            if result.is_person {
                s.people_total += 1;
                match result.people_quality_tier.as_deref() {
                    Some("high") => s.people_quality_high += 1,
                    Some("medium") => s.people_quality_medium += 1,
                    Some("low") => s.people_quality_low += 1,
                    Some("stub") => s.people_quality_stub += 1,
                    _ => {}
                }
            }

            // Samples near threshold (bulk_ratio 0.55-0.85)
            if result.bulk_ratio >= 0.55
                && result.bulk_ratio <= 0.85
                && s.near_threshold_samples.len() < MAX_SAMPLES
            {
                s.near_threshold_samples.push(result.clone());
            }

            // Potential false positives: flagged as extended/structural but high curation
            if (result.tier == "extended" || result.tier == "structural")
                && result.curation_score >= 4
                && s.false_positive_samples.len() < MAX_SAMPLES
            {
                s.false_positive_samples.push(result.clone());
            }

            // Tier samples
            match result.tier.as_str() {
                "core" if s.core_samples.len() < MAX_SAMPLES => {
                    s.core_samples.push(result.clone());
                }
                "extended" if s.extended_samples.len() < MAX_SAMPLES => {
                    s.extended_samples.push(result.clone());
                }
                "structural" if s.structural_samples.len() < MAX_SAMPLES => {
                    s.structural_samples.push(result.clone());
                }
                _ => {}
            }
        }
    });

    // Wait for reader
    reader_handle.join().unwrap();

    // Wait for workers
    for handle in worker_handles {
        handle.join().unwrap();
    }

    // Wait for collector
    collector_handle.join().unwrap();

    progress.finish_with_message("Done");

    // Finalize stats
    {
        let mut s = stats.lock();
        s.total_lines = line_count.load(Ordering::Relaxed);
        s.total_items = s.with_label; // We only produce results for labeled items
    }

    // Generate report
    let report = generate_report(&stats.lock(), &args.file);

    // Output
    match args.output {
        Some(ref path) => {
            let mut f = File::create(path)?;
            f.write_all(report.as_bytes())?;
            eprintln!("Report written to {:?}", path);
        }
        None => {
            print!("{}", report);
        }
    }

    Ok(())
}

// ============================================================
// Entity processing
// ============================================================

fn process_entity(
    line: &str,
    rtxn: &heed::RoTxn,
    lmdb_db: &Database<Str, Str>,
    lookup: &BulkLookup,
) -> Option<EntityResult> {
    let mut line_bytes = line.as_bytes().to_vec();
    let value: simd_json::OwnedValue = simd_json::to_owned_value(&mut line_bytes).ok()?;
    let obj = value.as_object()?;

    // Only items
    let entity_type = obj.get("type")?.as_str()?;
    if entity_type != "item" {
        return None;
    }

    let qid = obj.get("id")?.as_str()?.to_string();
    if qid.is_empty() {
        return None;
    }

    // English label from LMDB
    let label = lmdb_db.get(rtxn, &qid).ok().flatten()?.to_string();

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

    // Check if person (has Q5 in P31)
    let is_person = p31_values.iter().any(|v| v == "Q5");

    // PROPOSAL 1: P31-based structural detection (first rule, before any scoring)
    let is_structural = p31_values
        .iter()
        .any(|v| lookup.structural_p31_qids.contains(v));

    // Count bulk properties (exclude P31 from ratio computation)
    let props_for_ratio: HashSet<&String> = property_keys
        .iter()
        .filter(|p| p.as_str() != "P31")
        .collect();
    let total_props = props_for_ratio.len();

    if total_props == 0 {
        let (tier, tier_reason) = if is_structural {
            ("structural", "structural_p31")
        } else {
            ("core", "core")
        };
        return Some(EntityResult {
            qid,
            label,
            tier: tier.to_string(),
            tier_reason: tier_reason.to_string(),
            bulk_ratio: 0.0,
            curation_score: 0,
            total_props: 0,
            bulk_prop_count: 0,
            matched_sources: vec![],
            is_person,
            people_quality_tier: None,
            p31_values,
        });
    }

    // Count how many entity props belong to ANY source database
    let mut bulk_prop_count = 0;
    let mut matched_source_set: HashSet<String> = HashSet::new();

    for pid in &props_for_ratio {
        if lookup.all_bulk_props.contains(pid.as_str()) {
            bulk_prop_count += 1;
            if let Some(sources) = lookup.prop_to_sources.get(pid.as_str()) {
                for src in sources {
                    matched_source_set.insert(src.clone());
                }
            }
        }
    }

    let bulk_ratio = bulk_prop_count as f64 / total_props as f64;

    // Signature-based source database matching
    let mut signature_matched: Vec<String> = Vec::new();
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
            signature_matched.push(src_name.clone());
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
                    signature_matched.push(src_name.clone());
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

    // Determine tier using layered rules
    let (tier, tier_reason) = if is_structural {
        ("structural".to_string(), "structural_p31".to_string())
    } else {
        // Rule 2: Signature-based detection
        let mut signature_flagged = false;
        let mut signature_source = String::new();
        if !signature_matched.is_empty() && curation_score <= 1 {
            signature_flagged = true;
            signature_source = signature_matched[0].clone();
        }

        if signature_flagged {
            (
                "extended".to_string(),
                format!("signature:{}", signature_source),
            )
        } else if bulk_ratio >= lookup.bulk_ratio_threshold
            && curation_score < lookup.curation_score_protect
        {
            ("extended".to_string(), "ratio".to_string())
        } else {
            ("core".to_string(), "core".to_string())
        }
    };

    // People quality tier (adjusted thresholds: >= 12 high, >= 5 medium)
    let people_quality_tier = if is_person {
        let bio_count = property_keys
            .iter()
            .filter(|p| lookup.core_bio_props.contains(p.as_str()))
            .count();

        if curation_score >= 12 {
            Some("high".to_string())
        } else if curation_score >= 5 {
            Some("medium".to_string())
        } else if bio_count >= lookup.min_core_for_low {
            Some("low".to_string())
        } else {
            Some("stub".to_string())
        }
    } else {
        None
    };

    let matched_sources: Vec<String> = matched_source_set.into_iter().collect();

    Some(EntityResult {
        qid,
        label,
        tier,
        tier_reason,
        bulk_ratio,
        curation_score,
        total_props,
        bulk_prop_count,
        matched_sources,
        is_person,
        people_quality_tier,
        p31_values,
    })
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

// ============================================================
// Report generation
// ============================================================

fn generate_report(stats: &Stats, file_path: &PathBuf) -> String {
    let mut r = String::new();

    writeln!(r, "# Bulk Import Detection Report").unwrap();
    writeln!(r, "").unwrap();
    writeln!(r, "File: {:?}", file_path).unwrap();
    writeln!(r, "Generated: {}", chrono_now()).unwrap();
    writeln!(r, "").unwrap();

    // Overview
    writeln!(r, "## Overview").unwrap();
    writeln!(r, "").unwrap();
    writeln!(r, "| Metric | Count |").unwrap();
    writeln!(r, "|--------|-------|").unwrap();
    writeln!(r, "| Total lines | {} |", stats.total_lines).unwrap();
    writeln!(r, "| Entities with English label | {} |", stats.with_label).unwrap();
    writeln!(r, "").unwrap();

    // Tier distribution
    let total = stats.tier_core + stats.tier_extended + stats.tier_structural;
    writeln!(r, "## Tier Distribution").unwrap();
    writeln!(r, "").unwrap();
    writeln!(r, "| Tier | Count | % |").unwrap();
    writeln!(r, "|------|-------|---|").unwrap();
    if total > 0 {
        writeln!(
            r,
            "| core | {} | {:.1}% |",
            stats.tier_core,
            stats.tier_core as f64 / total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| extended | {} | {:.1}% |",
            stats.tier_extended,
            stats.tier_extended as f64 / total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| structural | {} | {:.1}% |",
            stats.tier_structural,
            stats.tier_structural as f64 / total as f64 * 100.0
        )
        .unwrap();
        writeln!(r, "| **total** | **{}** | |", total).unwrap();
    }
    writeln!(r, "").unwrap();

    // Source database hits
    writeln!(r, "## Source Database Hits").unwrap();
    writeln!(r, "").unwrap();
    writeln!(r, "| Source Database | Entities | % of labeled |").unwrap();
    writeln!(r, "|---------------|----------|-------------|").unwrap();
    let mut source_vec: Vec<(&String, &u64)> = stats.source_hits.iter().collect();
    source_vec.sort_by(|a, b| b.1.cmp(a.1));
    for (name, count) in &source_vec {
        if stats.with_label > 0 {
            writeln!(
                r,
                "| {} | {} | {:.1}% |",
                name,
                count,
                **count as f64 / stats.with_label as f64 * 100.0
            )
            .unwrap();
        }
    }
    writeln!(r, "").unwrap();

    // Bulk ratio distribution
    writeln!(r, "## Bulk Ratio Distribution").unwrap();
    writeln!(r, "").unwrap();
    writeln!(r, "| Range | Count | % |").unwrap();
    writeln!(r, "|-------|-------|---|").unwrap();
    let labels = [
        "0.0-0.1", "0.1-0.2", "0.2-0.3", "0.3-0.4", "0.4-0.5", "0.5-0.6", "0.6-0.7", "0.7-0.8",
        "0.8-0.9", "0.9-1.0",
    ];
    for (i, label) in labels.iter().enumerate() {
        let count = stats.bulk_ratio_histogram[i];
        if total > 0 {
            writeln!(
                r,
                "| {} | {} | {:.1}% |",
                label,
                count,
                count as f64 / total as f64 * 100.0
            )
            .unwrap();
        }
    }
    writeln!(r, "").unwrap();

    // Curation score distribution
    writeln!(r, "## Curation Score Distribution").unwrap();
    writeln!(r, "").unwrap();
    writeln!(r, "| Score | Count | % |").unwrap();
    writeln!(r, "|-------|-------|---|").unwrap();
    for i in 0..12 {
        let count = stats.curation_score_histogram[i];
        let label = if i < 11 {
            format!("{}", i)
        } else {
            "11+".to_string()
        };
        if total > 0 {
            writeln!(
                r,
                "| {} | {} | {:.1}% |",
                label,
                count,
                count as f64 / total as f64 * 100.0
            )
            .unwrap();
        }
    }
    writeln!(r, "").unwrap();

    // Detection rule breakdown
    writeln!(r, "## Detection Rule Breakdown").unwrap();
    writeln!(r, "").unwrap();
    writeln!(r, "| Rule | Count | % |").unwrap();
    writeln!(r, "|------|-------|---|").unwrap();
    if total > 0 {
        writeln!(
            r,
            "| structural_p31 | {} | {:.1}% |",
            stats.rule_structural_p31,
            stats.rule_structural_p31 as f64 / total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| signature | {} | {:.1}% |",
            stats.rule_signature,
            stats.rule_signature as f64 / total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| ratio | {} | {:.1}% |",
            stats.rule_ratio,
            stats.rule_ratio as f64 / total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| core (default) | {} | {:.1}% |",
            stats.rule_core,
            stats.rule_core as f64 / total as f64 * 100.0
        )
        .unwrap();
    }
    writeln!(r, "").unwrap();

    // Signature detections by source
    if !stats.signature_detections.is_empty() {
        writeln!(r, "### Signature Detections by Source").unwrap();
        writeln!(r, "").unwrap();
        writeln!(r, "| Source | Count |").unwrap();
        writeln!(r, "|--------|-------|").unwrap();
        let mut sig_vec: Vec<(&String, &u64)> = stats.signature_detections.iter().collect();
        sig_vec.sort_by(|a, b| b.1.cmp(a.1));
        for (name, count) in &sig_vec {
            writeln!(r, "| {} | {} |", name, count).unwrap();
        }
        writeln!(r, "").unwrap();
    }

    // People quality tiers
    if stats.people_total > 0 {
        writeln!(r, "## People Quality Tiers").unwrap();
        writeln!(r, "").unwrap();
        writeln!(r, "| Quality | Count | % of people |").unwrap();
        writeln!(r, "|---------|-------|-------------|").unwrap();
        writeln!(
            r,
            "| high | {} | {:.1}% |",
            stats.people_quality_high,
            stats.people_quality_high as f64 / stats.people_total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| medium | {} | {:.1}% |",
            stats.people_quality_medium,
            stats.people_quality_medium as f64 / stats.people_total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| low | {} | {:.1}% |",
            stats.people_quality_low,
            stats.people_quality_low as f64 / stats.people_total as f64 * 100.0
        )
        .unwrap();
        writeln!(
            r,
            "| stub | {} | {:.1}% |",
            stats.people_quality_stub,
            stats.people_quality_stub as f64 / stats.people_total as f64 * 100.0
        )
        .unwrap();
        writeln!(r, "| **total** | **{}** | |", stats.people_total).unwrap();
        writeln!(r, "").unwrap();
    }

    // Sample: Near threshold entities
    if !stats.near_threshold_samples.is_empty() {
        writeln!(
            r,
            "## Sample: Near-Threshold Entities (bulk_ratio 0.55-0.85)"
        )
        .unwrap();
        writeln!(r, "").unwrap();
        write_entity_table(&mut r, &stats.near_threshold_samples);
    }

    // Sample: Potential false positives
    if !stats.false_positive_samples.is_empty() {
        writeln!(
            r,
            "## Sample: Potential False Positives (flagged bulk + high curation)"
        )
        .unwrap();
        writeln!(r, "").unwrap();
        write_entity_table(&mut r, &stats.false_positive_samples);
    }

    // Sample: Core entities
    if !stats.core_samples.is_empty() {
        writeln!(r, "## Sample: Core Tier Entities").unwrap();
        writeln!(r, "").unwrap();
        write_entity_table(&mut r, &stats.core_samples);
    }

    // Sample: Extended entities
    if !stats.extended_samples.is_empty() {
        writeln!(r, "## Sample: Extended Tier Entities").unwrap();
        writeln!(r, "").unwrap();
        write_entity_table(&mut r, &stats.extended_samples);
    }

    // Sample: Structural entities
    if !stats.structural_samples.is_empty() {
        writeln!(r, "## Sample: Structural Tier Entities").unwrap();
        writeln!(r, "").unwrap();
        write_entity_table(&mut r, &stats.structural_samples);
    }

    r
}

fn write_entity_table(r: &mut String, entities: &[EntityResult]) {
    writeln!(r, "| QID | Label | Tier | Reason | Bulk Ratio | Curation | Props | Bulk Props | Sources | Person | Quality |").unwrap();
    writeln!(r, "|-----|-------|------|--------|-----------|----------|-------|------------|---------|--------|---------|").unwrap();
    for e in entities {
        let label_short = if e.label.len() > 30 {
            format!("{}...", &e.label[..27])
        } else {
            e.label.clone()
        };
        let sources_short = if e.matched_sources.len() > 3 {
            format!(
                "{}+{}",
                e.matched_sources[..3].join(","),
                e.matched_sources.len() - 3
            )
        } else {
            e.matched_sources.join(",")
        };
        let quality = e.people_quality_tier.as_deref().unwrap_or("-");
        writeln!(
            r,
            "| {} | {} | {} | {} | {:.2} | {} | {} | {} | {} | {} | {} |",
            e.qid,
            label_short,
            e.tier,
            e.tier_reason,
            e.bulk_ratio,
            e.curation_score,
            e.total_props,
            e.bulk_prop_count,
            sources_short,
            if e.is_person { "Y" } else { "N" },
            quality,
        )
        .unwrap();
    }
    writeln!(r, "").unwrap();
}

fn chrono_now() -> String {
    // Simple timestamp without adding chrono dependency
    "2026-02-09".to_string()
}
