# AIDA-YAGO Benchmark

AIDA CoNLL-YAGO dataset — the standard benchmark for entity linking, combining CoNLL-2003 NER annotations with AIDA-YAGO2 entity links to Wikipedia/Freebase/YAGO.

## Dataset

The merged dataset combines:
- **AIDA-YAGO2** annotations: entity mentions linked to Wikipedia URLs, YAGO names, and Freebase MIDs
- **CoNLL-2003** NER types: PER, LOC, ORG, MISC (from HuggingFace `eriktks/conll2003` parquets)

### Merged Format

`aida_conll_merged.tsv` — tab-separated, one token per line:

| Column | Description |
|--------|-------------|
| doc_id | Document number (1–1393) |
| split | train / testa (dev) / testb (test) |
| sentence_id | Sentence index within document |
| token_idx | Token index within sentence |
| token | Word text |
| ner_tag | NER tag (BIO: B-PER, I-ORG, O, etc.) |
| mention | Entity mention text (B-/I- tokens only) |
| yago_name | YAGO entity name |
| wikipedia_url | Full Wikipedia URL |
| freebase_mid | Freebase MID (e.g., /m/0345h) |

Split ranges: docs 1–946 = train, 947–1162 = testa (validation), 1163–1393 = testb (test).

### Source

- [AIDA CoNLL-YAGO Dataset](https://www.mpi-inf.mpg.de/departments/databases-and-information-systems/research/ambiverse-nlu/aida/downloads) (Hoffart et al., EMNLP 2011)
- CoNLL-2003 NER: `eriktks/conll2003` on HuggingFace

## Entity Resolution

`resolve_entities.py` resolves Wikipedia page titles from AIDA annotations to Wikidata QIDs with P31/P279 type information. It first checks the local classified parquet (`data_dump_core_output/classified_with_sitelinks.parquet`, covers ~88%), then falls back to the Wikidata API for the rest, including Wikipedia redirect resolution.

`classified_with_sitelinks.parquet` is expected to come from the HuggingFace release artifacts, not from the default classifier output generated locally in this repo.

```bash
python resolve_entities.py           # full run (parquet + API)
python resolve_entities.py --no-fetch  # parquet-only, no API calls
```

**Results**: 5,595 / 5,598 resolved (99.95%). 3 unresolved are deleted Wikipedia articles:
- `Camelot,_Chesapeake,_Virginia`
- `International_cricketers_of_South_African_origin`
- `U._Chandana`

## Classification Evaluation

`eval_classification.py` classifies all AIDA entities using the YAML gate definitions and compares against NER tags. Generates `classification_report.md` with confusion matrix, misalignment analysis by pattern, and unclassified P31 types.

```bash
uv run --project src/python python benchmarks/aida_yago/eval_classification.py          # print summary
uv run --project src/python python benchmarks/aida_yago/eval_classification.py --report  # also generate report
```

## Files

| File | Purpose |
|------|---------|
| `data/AIDA-YAGO2-dataset.tsv` | Original AIDA-YAGO2 annotations |
| `data/conll2003_{train,test,validation}.parquet` | CoNLL-2003 NER from HuggingFace |
| `data/aida_conll_merged.tsv` | Merged AIDA + CoNLL-2003 (301K tokens) |
| `data/aida_entities.tsv` | Resolved entities: wikipedia_title, qid, label, description, p31/p279 QIDs and labels |
| `merge_aida_conll.py` | Merges AIDA annotations with CoNLL-2003 NER types |
| `resolve_entities.py` | Resolves Wikipedia titles → Wikidata QIDs with P31/P279 |
| `eval_classification.py` | Evaluates YAML classification against NER tags, generates report |
| `classification_report.md` | Auto-generated evaluation report (re-run `--report` to update) |
