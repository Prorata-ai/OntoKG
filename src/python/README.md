# OntoKG - Python Implementation

Python implementation of the Wikidata entity classification pipeline and analysis tools.

## Installation

Requires Python 3.12+. Install with [uv](https://github.com/astral-sh/uv) (recommended) or pip:

```bash
cd src/python
uv sync          # or: pip install -e .
```

## Core Pipeline

All commands run from the project root.

| Script | Description |
|--------|-------------|
| `tag_loader.py` | YAML tag loader and `classify_raw()` API |
| `classify_dump.py` | Classify JSONL.zst dumps into 13-column Parquet files |
| `filter_core.py` | Filter full dump to keep only core-tier entities |
| `detect_bulk.py` | Detect bulk-imported entities using `src/bulk_import.yaml` |
| `export_neo4j.py` | Export classified Parquets to Neo4j CSV (nodes + edges + stubs) |
| `extract_category.py` | Extract a single category from classified Parquets |
| `export_labels.py` | Export QID/PID labels from LMDB to JSON |
| `convert_labels.py` | Convert label lookup table between LMDB and Parquet formats |

```bash
# Example: classify a dump shard
uv run --project src/python python src/python/classify_dump.py \
  --input data_dump_core/shard_0001.jsonl.zst \
  --output data_dump_core_output/classified_python/shard_0001.parquet
```

## Grounding Tools

Tools used in the agentic LLM workflow for iterative schema refinement (Section 4 of the paper).

| Script | Description |
|--------|-------------|
| `query_lmdb.py` | Look up QID/PID English labels from LMDB |
| `query_p31.py` | Query Wikidata SPARQL endpoint for P31 values |
| `tag_validator.py` | Validate YAML tag structure, QID/PID format, gate exclusivity |
| `analyze_category_p31.py` | P31 distribution analysis per category (DuckDB) |
| `find_unclassified_hubs.py` | Find high-reference unclassified hub entities |

```bash
# P31 analysis for a category
uv run --project src/python python src/python/analyze_category_p31.py \
  --input data_dump_output/entity_science.parquet --category science

# Look up entity labels
uv run --project src/python python src/python/query_lmdb.py Q42 P31
```

## Demos

Application demos in `demos/` showing downstream uses of the classification schema.

| Script | Description |
|--------|-------------|
| `disambiguate.py` | Entity disambiguation using module fingerprints |
| `llm_extract_test.py` | LLM-guided entity extraction from text |
| `property_organizer.py` | Property organization: raw Wikidata → structured view (live API) |

```bash
# Entity disambiguation
uv run --project src/python python src/python/demos/disambiguate.py

# LLM entity extraction (uses bundled data/wiki_extracts.json by default)
uv run --project src/python python src/python/demos/llm_extract_test.py --limit 5
```

Outputs are written to `data_demos/` (created automatically). Requires `OPENAI_API_KEY`.

## API Usage

```python
from tag_loader import TagLoader

loader = TagLoader()

# Classify by P31 values
result = loader.classify_by_p31(["Q5"])  # human → people

# Full classification from raw claims
result = loader.classify_raw({
    "P31": [{"value": "Q5"}],
    "P39": [{"value": "Q11696"}],
})
# result.category = "people", result.modules = ["politics"]
```
