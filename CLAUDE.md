# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reproducibility package for the paper *"OntoKG: Ontology-Oriented Knowledge Graph Construction with Intrinsic-Relational Routing"*.

Wikidata entity classification system that processes full Wikidata dumps (100.9M entities) and categorizes them into 8 top-level categories with 94 modules (56 intrinsic + 38 relational) using declarative YAML tag definitions. Each module has a `type` (intrinsic or relational) that determines its role in the property graph: intrinsic modules become node attributes (e.g., PostgreSQL columns), relational modules become graph edges (e.g., Neo4j relationships). The pipeline: Wikidata JSONL dumps → bulk import filtering → Rust classifier → Parquet files → property graph export (Neo4j CSVs).

**Key numbers** (Wikidata dump 2026-01-19):

| Metric | Value |
|--------|-------|
| Core dump entities | 34,601,494 |
| Classified | 32,294,548 (93.3%) |
| With modules | 31,662,189 (91.5% of total, 98.0% of classified) |
| Unclassified | 2,306,946 (6.7%) |
| Neo4j nodes | 32,294,548 + 1,711,114 stubs = 34,005,662 |
| Neo4j edges | 61,179,080 (38 edge files) |
| Categories | 8 |
| Modules | 94 (56 intrinsic + 38 relational) |

## Quick Reference

```bash
# Build Rust binaries
cd src/rust && cargo build --release

# Run classification (defaults to data_dump_core → data_dump_core_output/classified_rust)
./src/rust/target/release/classify_dump

# Build LMDB label store from Wikidata dump
./src/rust/target/release/build_lmdb

# Bulk import detection and core filtering
./src/rust/target/release/detect_bulk --file data_dump/wikidata-20260119-all_0001.jsonl.zst
./src/rust/target/release/filter_core

# Export Neo4j CSV files (nodes + edges + stubs)
./src/rust/target/release/export_neo4j

# Validate YAML tags
./src/rust/target/release/tag_validator --gate-exclusive
uv run --project src/python python src/python/tag_validator.py

# Python scripts (always from project root)
uv run --project src/python python src/python/analyze_category_p31.py \
  --input data_dump_output/entity_science.parquet --category science
uv run --project src/python python src/python/query_lmdb.py Q42 Q31

# Run benchmarks
uv run --project src/python python benchmarks/aida_yago/eval_classification.py
uv run --project src/python python benchmarks/cleanconll/eval_classification.py

# Lint Python
uv run --project src/python ruff check src/python/ --fix
```

## Architecture

### Data Flow

```
data_dump/*.jsonl.zst  →  detect_bulk / filter_core  →  data_dump_core/*.jsonl.zst
    ↓
build_lmdb  →  labels_en.lmdb (QID/PID → English label)
    ↓
classify_dump (Rust)  →  classified_rust/*.parquet (13 columns)
    ↓                         ↓
export_neo4j              extract_category / count_categories / analyze_category_p31
    ↓
neo4j/ (nodes_{cat}.csv + edges_{module}.csv + nodes_stub.csv)
```

### Key Components

- **`src/tags/*.yaml`** — Declarative category definitions. Each file defines `gate_qids` (entry criteria via P31/P279), `core` props, and `modules` with type (intrinsic/relational), indicators, and value_props. This is the primary configuration surface.
- **`src/rust/src/loader.rs`** — Classification engine. P31-first matching with P279 fallback: iterates entity's P31 values in order, matches first gate QID hit; if no P31 match, falls back to P279 (subclass of) values against the same gate QIDs.
- **`src/rust/src/bin/classify_dump.rs`** — Production pipeline. Parallel processing (4 file workers × 16 entity workers), ZSTD decompression, simd-json parsing, LMDB English label filtering, 13-column Parquet output with claims split into core/intrinsic/relational/unclaimed, checkpointing.
- **`src/python/classify_dump.py`** — Single-threaded Python classifier producing identical 13-column Parquet output. Uses `TagLoader.classify_raw()` for classification from raw claims. ~10x slower than Rust.
- **`src/rust/src/bin/tag_validator.rs`** / **`src/python/tag_validator.py`** — YAML schema validation. Checks QID/PID format, module type, P31-in-gate_qids, P106 exclusivity, Jaccard overlap, gate_exclusive mode.
- **`src/python/analyze_category_p31.py`** — DuckDB-powered analysis producing coverage stats, P31 frequency tables, P31×module crosstabs, and unmodulated entity reports.
- **`src/rust/src/bin/export_neo4j.rs`** / **`src/python/export_neo4j.py`** — Exports classified parquets to CSV for `neo4j-admin database import full`. Three passes: (1) per-category node CSVs, (2) per-module edge CSVs with qualifiers, (3) stub nodes for edge targets not in dataset.
- **`src/python/find_unclassified_hubs.py`** — Finds high-reference unclassified "hub" entities using DuckDB. Groups by P31 type, ranks by inbound reference count.
- **`src/python/demos/`** — Application demos: entity disambiguation via module fingerprints (`disambiguate.py`), LLM entity extraction (`llm_extract_test.py`), property organization showing raw Wikidata → structured view via live API (`property_organizer.py`).
- **`benchmarks/`** — NER benchmark evaluation and entity disambiguation.
  - `resolve_common.py` — Shared entity resolution pipeline (99.9%+ resolution rate).
  - `aida_yago/` — AIDA-YAGO evaluation (94.7% classified, 93.1% aligned).
  - `cleanconll/` — CleanCoNLL evaluation (93.9% classified, 97.8% aligned).
  - `blink/` — Controlled BLINK entity disambiguation (12,323 mentions). Compares YAGO-4.5 types vs OntoKG module-based types using Voyage rerank-2.5. Includes precomputed results for paper table reproduction.
- **`viz_core/`** — Interactive D3.js dashboard (Overview, Distribution, Network, Hierarchy, Schema). Serve from project root: `python3 -m http.server 8080`, open `http://localhost:8080/viz_core/`.

### Classified Parquet Schema

13-column output from both Rust and Python classifiers:

| Column | Type | Description |
|--------|------|-------------|
| qid | VARCHAR | Wikidata entity ID (e.g., Q42) |
| label | VARCHAR | English label from LMDB |
| description | VARCHAR | English description (fallback: en → en-us → en-gb → mul) |
| aliases | VARCHAR (JSON) | JSON array of English aliases |
| p31_values | VARCHAR[] | Array of P31 (instance of) QID values |
| category | VARCHAR | Top-level category name |
| intrinsic_modules | VARCHAR[] | Intrinsic modules matched |
| relational_modules | VARCHAR[] | Relational modules matched |
| gate_matched | VARCHAR | The gate QID that matched this entity |
| gate_property | VARCHAR | Which property triggered the match (P31 or P279) |
| matched_indicators | VARCHAR (JSON) | Module indicators matched. Presence: `PID`; value-based: `PID:QID` |
| claims | VARCHAR (JSON) | Organized claims: `{"core":{}, "intrinsic":{}, "relational":{}, "unclaimed":{}}` |
| sitelinks | VARCHAR (JSON) | Wikipedia sitelinks |

## YAML Update Rules

When editing `src/tags/*.yaml`:
- Every QID/PID must have an inline comment matching its LMDB label exactly. Look up labels with `query_lmdb.py` before adding.
- Module names: single noun only (no phrases or multi-word names).
- Every module must have a `type: intrinsic` or `type: relational` field.
- Any QID in a module's P31 indicators must also appear in `gate_qids`.
- Run `tag_validator.py` after changes. Use `--gate-exclusive` to check for QID conflicts across categories.

## Key Design Decisions

- **P31-first matching with P279 fallback**: Classification iterates P31 values in Wikidata's listed order and matches the first gate hit. If no P31 match, falls back to P279 (subclass of) values against the same gate QIDs.
- **Gate-module sync**: If a QID added to `gate_qids` fits an existing module, add it to that module's P31 indicators too. Some QIDs don't fit any module — that's fine, they still get classified into the category.
- **Module types**: `intrinsic` modules are category-unique and store entity node properties. `relational` modules appear in 2+ categories and become graph edges. The `type` field in YAML is the source of truth.
- **Cross-cutting relational modules**: Properties appearing across many modules are extracted into dedicated relational modules (e.g., `authorship`, `location`, `hierarchy`, `operation`). This avoids duplication and creates clean graph edges.
- **Neo4j node labels**: Category names map to short labels: people→PERSON, places→PLACE, science→SCIENCE, creative_works_media→WORK, organizations→ORGANIZATION, events_actions→EVENT, knowledge_information_abstracts→KNOWLEDGE, products_artifacts→PRODUCT.
- **English-only**: Only entities with English labels in LMDB are processed.

