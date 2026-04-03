use heed::{types::Str, Database, EnvOpenOptions};
use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = PathBuf::from("../../data_dump_output/labels_en.lmdb");

    println!("Opening LMDB at {:?}", path);

    let env = unsafe {
        EnvOpenOptions::new()
            .max_dbs(1)
            .map_size(20 * 1024 * 1024 * 1024)
            .open(&path)?
    };

    println!("Environment opened");

    let rtxn = env.read_txn()?;
    println!("Read transaction created");

    let db: Database<Str, Str> = env.open_database(&rtxn, Some("labels_en"))?.unwrap();
    println!("Database opened");

    // Test a few QIDs
    for qid in &["Q5", "Q76", "Q42", "Q1", "Q100", "Q1000"] {
        match db.get(&rtxn, qid) {
            Ok(Some(label)) => println!("{}: {}", qid, label),
            Ok(None) => println!("{}: NOT FOUND", qid),
            Err(e) => println!("{}: ERROR {:?}", qid, e),
        }
    }

    // Count total entries
    let count = db.iter(&rtxn)?.take(10).count();
    println!("\nFirst 10 entries sampled, count: {}", count);

    // Show first 5 entries
    println!("\nFirst 5 entries:");
    for result in db.iter(&rtxn)?.take(5) {
        let (k, v) = result?;
        println!("  {}: {}", k, v);
    }

    Ok(())
}
