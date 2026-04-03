# OntoKG - Rust Implementation

High-performance Rust implementation of the Wikidata entity classification pipeline. Processes ~110,000 entities/second on commodity hardware.

## Build

Requires Rust 1.75+.

```bash
cd src/rust && cargo build --release
```

## Binaries

All binaries are built to `src/rust/target/release/`. Run from the project root.

| Binary | Description |
|--------|-------------|
| `classify_dump` | Entity classification (~110K ent/sec, parallel) |
| `build_lmdb` | Build LMDB label store from Wikidata dump |
| `filter_core` | Filter dump to keep only core-tier entities |
| `detect_bulk` | Detect bulk-imported entities |
| `export_neo4j` | Export classified Parquets to Neo4j CSV |
| `extract_category` | Extract a single category from classified Parquets |
| `count_categories` | Aggregate entity/module counts per category |
| `tag_validator` | Validate YAML tag structure and consistency |

## Usage

### Build LMDB Label Store

Scans the full Wikidata dump and extracts English labels (fallback: en → en-us → en-gb → mul) for all QIDs/PIDs.

```bash
./src/rust/target/release/build_lmdb
```

| Option | Default | Description |
|--------|---------|-------------|
| `--dump-dir` | `data_dump` | Directory with `.jsonl.zst` dump files |
| `--output` | `data_dump_output/labels_en.lmdb` | Output LMDB path |
| `--workers` | `4` | Parallel file workers |

Performance: 118.7M records scanned, 100.9M labels extracted, ~11 min (12 workers, Apple M-series).

### Classify Dump

Parallel entity classifier. Reads `.jsonl.zst` files, classifies via YAML tags, outputs 13-column Parquet.

```bash
# Full run (all shards)
./src/rust/target/release/classify_dump

# Specific shards
./src/rust/target/release/classify_dump --files 0001,0003,0007

# Range
./src/rust/target/release/classify_dump --start 5 --end 10

# Force reprocess
./src/rust/target/release/classify_dump --files 0001 --force
```

| Option | Default | Description |
|--------|---------|-------------|
| `--dump-dir` | `data_dump` | Input `.jsonl.zst` directory |
| `--lmdb` | `data_dump_output/labels_en.lmdb` | LMDB label database |
| `--tags-dir` | `src/tags` | YAML tag definitions |
| `--output` | `data_dump_output/classified_rust` | Output Parquet directory |
| `--file-workers` | `4` | Files processed in parallel |
| `--workers` | `16` | Workers per file |
| `--files` | - | Specific file IDs (comma-separated) |
| `--start` / `--end` | - | File range (inclusive) |
| `--force` | - | Reprocess even if completed |
| `--restart` | - | Clear checkpoint, start fresh |

Saves progress to `checkpoint.json` in the output directory. Resumes automatically if interrupted.

### Filter Core Entities

```bash
./src/rust/target/release/filter_core
./src/rust/target/release/detect_bulk --file data_dump/wikidata-20260119-all_0001.jsonl.zst
```

### Export Neo4j CSV

Three-pass export: (1) per-category node CSVs, (2) per-module edge CSVs, (3) stub nodes.

```bash
./src/rust/target/release/export_neo4j
./src/rust/target/release/export_neo4j --limit 3   # first 3 shards only
```

### Validate YAML Tags

```bash
./src/rust/target/release/tag_validator
./src/rust/target/release/tag_validator --gate-exclusive --verbose
```

## Library

| File | Description |
|--------|-------------|
| `src/loader.rs` | Classification engine (P31-first, P279 fallback) |
| `src/model.rs` | Data models (`Entity`, `ClassificationResult`, etc.) |
| `src/validator.rs` | YAML schema validation |
| `src/error.rs` | Error types |
| `src/util.rs` | Utilities |

### API Example

```rust
use wikidata_tags::TagLoader;

let loader = TagLoader::new(Some("../tags".into())).unwrap();

// Classify by P31
let result = loader.classify_by_p31(&["Q5".to_string()]);
println!("{}", result.category); // "people"
```

### Run Examples

```bash
cargo run --example example
cargo run --example benchmark
```
