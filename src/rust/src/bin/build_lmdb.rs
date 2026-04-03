//! Build LMDB label database from Wikidata JSON dump files.
//! Extracts English labels (fallback: en → en-us → en-gb → mul)
//! for all Q* and P* entities, writing QID/PID → label pairs.
//!
//! Parallelizes across files for maximum throughput.

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use anyhow::Result;
use clap::Parser;
use crossbeam_channel::bounded;
use heed::types::Str;
use heed::{Database, EnvOpenOptions};
use indicatif::{MultiProgress, ProgressBar, ProgressStyle};
use rayon::prelude::*;
use simd_json::prelude::*;

#[derive(Parser, Debug)]
#[command(name = "build_lmdb")]
#[command(about = "Build LMDB label database from Wikidata JSON dump")]
struct Args {
    /// Directory with .jsonl.zst dump files
    #[arg(long, default_value = "data_dump")]
    dump_dir: PathBuf,

    /// Output LMDB directory
    #[arg(long, default_value = "data_dump_output/labels_en.lmdb")]
    output: PathBuf,

    /// LMDB map size in GB
    #[arg(long, default_value = "16")]
    map_size_gb: usize,

    /// Number of file workers
    #[arg(long, default_value = "4")]
    workers: usize,
}

const FALLBACK: &[&str] = &["en", "en-us", "en-gb", "mul"];
const WRITE_BATCH: usize = 100_000;

fn resolve_label(labels_obj: &simd_json::OwnedValue) -> Option<String> {
    let obj = labels_obj.as_object()?;
    for lang in FALLBACK {
        if let Some(entry) = obj.get(*lang) {
            return entry.as_object()?.get("value")?.as_str().map(String::from);
        }
    }
    None
}

/// Parse a single line, return (id, label) if found.
#[inline]
fn parse_line(mut bytes: Vec<u8>) -> Option<(String, String)> {
    let parsed = simd_json::to_owned_value(&mut bytes).ok()?;
    let obj = parsed.as_object()?;
    let id = obj.get("id")?.as_str()?;
    if !id.starts_with('Q') && !id.starts_with('P') {
        return None;
    }
    let label = resolve_label(obj.get("labels")?)?;
    Some((id.to_string(), label))
}

fn main() -> Result<()> {
    let args = Args::parse();
    let start = Instant::now();

    // Collect input files
    let mut files: Vec<PathBuf> = fs::read_dir(&args.dump_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.extension().map_or(false, |ext| ext == "zst")
                && p.to_string_lossy().contains(".jsonl")
        })
        .collect();
    files.sort();

    if files.is_empty() {
        anyhow::bail!("No .jsonl.zst files found in {:?}", args.dump_dir);
    }
    println!("Found {} dump files in {:?}", files.len(), args.dump_dir);
    println!("Workers: {}", args.workers);

    // Create output directory and LMDB environment
    fs::create_dir_all(&args.output)?;
    let env = unsafe {
        EnvOpenOptions::new()
            .map_size(args.map_size_gb * 1024 * 1024 * 1024)
            .max_dbs(1)
            .open(&args.output)?
    };
    let mut wtxn = env.write_txn()?;
    let db: Database<Str, Str> = env.create_database(&mut wtxn, Some("labels_en"))?;
    wtxn.commit()?;
    println!("LMDB: {:?} (map: {} GB)", args.output, args.map_size_gb);

    // Stats
    let total_records = Arc::new(AtomicU64::new(0));
    let total_labels = Arc::new(AtomicU64::new(0));
    let total_missing = Arc::new(AtomicU64::new(0));

    // Channel: file workers send batches of (key, value) to single writer
    let (tx, rx) = bounded::<Vec<(String, String)>>(args.workers * 2);

    // Writer thread
    let env_writer = env.clone();
    let written = Arc::new(AtomicU64::new(0));
    let written_clone = written.clone();
    let writer = std::thread::spawn(move || -> Result<()> {
        let db: Database<Str, Str> = {
            let rtxn = env_writer.read_txn()?;
            env_writer.open_database(&rtxn, Some("labels_en"))?.unwrap()
        };
        let mut buf = Vec::with_capacity(WRITE_BATCH);
        for chunk in rx {
            buf.extend(chunk);
            if buf.len() >= WRITE_BATCH {
                let mut wtxn = env_writer.write_txn()?;
                for (k, v) in &buf {
                    db.put(&mut wtxn, k, v)?;
                }
                wtxn.commit()?;
                written_clone.fetch_add(buf.len() as u64, Ordering::Relaxed);
                buf.clear();
            }
        }
        if !buf.is_empty() {
            let mut wtxn = env_writer.write_txn()?;
            for (k, v) in &buf {
                db.put(&mut wtxn, k, v)?;
            }
            wtxn.commit()?;
            written_clone.fetch_add(buf.len() as u64, Ordering::Relaxed);
        }
        Ok(())
    });

    // Progress
    let mp = MultiProgress::new();
    let total_bytes: u64 = files.iter().map(|f| fs::metadata(f).map(|m| m.len()).unwrap_or(0)).sum();
    let overall = mp.add(ProgressBar::new(total_bytes));
    overall.set_style(
        ProgressStyle::default_bar()
            .template("{spinner:.green} [{elapsed_precise}] [{bar:50}] {bytes}/{total_bytes} ({eta}) | {msg}")
            .unwrap()
            .progress_chars("██░"),
    );
    overall.set_message("processing...");

    // Process files in parallel with rayon
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(args.workers)
        .build()?;

    let stats_records = total_records.clone();
    let stats_labels = total_labels.clone();
    let stats_missing = total_missing.clone();

    pool.install(|| {
        files.par_iter().for_each(|file| {
            let file_size = fs::metadata(file).map(|m| m.len()).unwrap_or(0);

            let f = match fs::File::open(file) {
                Ok(f) => f,
                Err(_) => return,
            };
            let decoder = match zstd::Decoder::new(f) {
                Ok(d) => d,
                Err(_) => return,
            };
            let reader = BufReader::with_capacity(2 * 1024 * 1024, decoder);

            let mut batch: Vec<(String, String)> = Vec::with_capacity(50_000);
            let mut local_records: u64 = 0;
            let mut local_labels: u64 = 0;
            let mut local_missing: u64 = 0;

            for line in reader.lines() {
                let line = match line {
                    Ok(l) if !l.is_empty() => l,
                    _ => continue,
                };
                local_records += 1;

                if let Some(pair) = parse_line(line.into_bytes()) {
                    local_labels += 1;
                    batch.push(pair);
                } else {
                    local_missing += 1;
                }

                if batch.len() >= 50_000 {
                    let _ = tx.send(std::mem::replace(&mut batch, Vec::with_capacity(50_000)));
                }
            }

            if !batch.is_empty() {
                let _ = tx.send(batch);
            }

            stats_records.fetch_add(local_records, Ordering::Relaxed);
            stats_labels.fetch_add(local_labels, Ordering::Relaxed);
            stats_missing.fetch_add(local_missing, Ordering::Relaxed);
            overall.inc(file_size);
        });
    });

    drop(tx);
    overall.finish_with_message("done");
    writer.join().unwrap()?;

    let elapsed = start.elapsed();
    let records = total_records.load(Ordering::Relaxed);
    let labels = total_labels.load(Ordering::Relaxed);
    let missing = total_missing.load(Ordering::Relaxed);
    let w = written.load(Ordering::Relaxed);

    println!("\n=== Build LMDB Complete ===");
    println!("Records scanned:  {}", records);
    println!("Labels found:     {} ({:.1}%)", labels, labels as f64 / records.max(1) as f64 * 100.0);
    println!("Missing labels:   {} ({:.1}%)", missing, missing as f64 / records.max(1) as f64 * 100.0);
    println!("LMDB written:     {}", w);
    println!("Fallback:         {:?}", FALLBACK);
    println!("Time:             {:.0}s ({:.1} min)", elapsed.as_secs_f64(), elapsed.as_secs_f64() / 60.0);
    println!("Output:           {:?}", args.output);

    Ok(())
}
