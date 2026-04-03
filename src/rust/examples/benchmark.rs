use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use serde_json::Value;
use tags::{Entity, TagLoader};

fn parse_wikidata_entity(qid: &str, raw: &Value) -> Entity {
    let mut properties: HashMap<String, Vec<String>> = HashMap::new();

    let claims = match raw.get("claims").and_then(Value::as_object) {
        Some(claims) => claims,
        None => {
            return Entity {
                qid: qid.to_string(),
                properties,
            }
        }
    };

    for (pid, claim_list) in claims {
        let mut values: Vec<String> = Vec::new();

        let claim_list = match claim_list.as_array() {
            Some(list) => list,
            None => continue,
        };

        for claim in claim_list {
            let mainsnak = match claim.get("mainsnak").and_then(Value::as_object) {
                Some(mainsnak) => mainsnak,
                None => continue,
            };

            let datavalue = match mainsnak.get("datavalue").and_then(Value::as_object) {
                Some(datavalue) => datavalue,
                None => continue,
            };

            let value = match datavalue.get("value") {
                Some(value) => value,
                None => continue,
            };

            match value {
                Value::Object(map) => {
                    if let Some(id) = map.get("id").and_then(Value::as_str) {
                        values.push(id.to_string());
                    } else if let Some(time) = map.get("time").and_then(Value::as_str) {
                        values.push(time.to_string());
                    } else if let Some(amount) = map.get("amount").and_then(Value::as_str) {
                        values.push(amount.to_string());
                    } else if let Some(text) = map.get("text").and_then(Value::as_str) {
                        values.push(text.to_string());
                    }
                }
                Value::String(s) => values.push(s.to_string()),
                _ => {}
            }
        }

        if !values.is_empty() {
            properties.insert(pid.to_string(), values);
        }
    }

    Entity {
        qid: qid.to_string(),
        properties,
    }
}

fn load_all_entities(data_dir: &Path) -> Vec<Entity> {
    let sample_files = [
        "wikidata_people_sample_100.json",
        "wikidata_organizations_sample_100.json",
        "wikidata_places_sample_100.json",
        "wikidata_events_sample_100.json",
        "wikidata_creative_works_sample_100.json",
        "wikidata_products_sample_100.json",
        "wikidata_knowledge_sample_100.json",
        "wikidata_science_sample_v1.json",
    ];

    let mut entities = Vec::new();

    for filename in sample_files {
        let path = data_dir.join(filename);
        if !path.exists() {
            continue;
        }

        let data = match fs::read(&path) {
            Ok(contents) => contents,
            Err(_) => continue,
        };

        let parsed: Value = match serde_json::from_slice(&data) {
            Ok(value) => value,
            Err(_) => continue,
        };

        let raw_entities = parsed.get("entities").unwrap_or(&parsed);
        let raw_entities = match raw_entities.as_object() {
            Some(map) => map,
            None => continue,
        };

        for (qid, raw_entity) in raw_entities {
            let entity = parse_wikidata_entity(qid, raw_entity);
            entities.push(entity);
        }
    }

    entities
}

fn benchmark_load(tags_dir: &Path, iterations: usize) -> f64 {
    let start = Instant::now();
    for _ in 0..iterations {
        let _ = TagLoader::new(Some(tags_dir.to_path_buf()));
    }
    let elapsed = start.elapsed();
    elapsed.as_secs_f64() * 1000.0 / iterations as f64
}

fn benchmark_classify(
    loader: &TagLoader,
    entities: &[Entity],
    iterations: usize,
) -> (f64, usize, f64) {
    let mut total = 0usize;
    let start = Instant::now();

    for _ in 0..iterations {
        for entity in entities {
            loader.classify(entity);
            total += 1;
        }
    }

    let elapsed = start.elapsed();
    let elapsed_sec = elapsed.as_secs_f64();
    let time_per_entity = (elapsed.as_secs_f64() * 1_000_000.0) / total as f64;

    (time_per_entity, total, elapsed_sec)
}

fn main() {
    let tags_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../tags");
    let data_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../data");

    println!("============================================================");
    println!("Rust Benchmark (Real Sample Data)");
    println!("============================================================");

    println!("Loading entities from data folder...");
    let entities = load_all_entities(&data_dir);
    println!("Loaded {} entities", entities.len());
    println!();

    if entities.is_empty() {
        println!(
            "No entities found. Ensure sample JSON files exist in {:?}",
            data_dir
        );
        return;
    }

    let loader = match TagLoader::new(Some(tags_dir.clone())) {
        Ok(loader) => loader,
        Err(err) => {
            println!("Error: {}", err);
            return;
        }
    };

    for entity in entities.iter().take(100) {
        loader.classify(entity);
    }

    let load_time = benchmark_load(&tags_dir, 10);
    println!("Tag Loading:     {:.3} ms per load", load_time);

    let (classify_time, total, elapsed) = benchmark_classify(&loader, &entities, 3);
    println!("Classification:  {:.3} µs per entity", classify_time);

    let throughput = total as f64 / elapsed;
    println!("Throughput:      {:.0} classifications/sec", throughput);

    println!();
    println!(
        "Total: {} classifications in {:.2} ms",
        total,
        elapsed * 1000.0
    );
}
