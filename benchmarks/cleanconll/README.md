# CleanCoNLL Benchmark

Corrected version of the CoNLL-03 NER dataset with improved labels and Wikipedia entity linking annotations (Rucker & Akbik, EMNLP 2023).

## Dataset

CleanCoNLL semi-automatically corrects ~7% of the original CoNLL-03 labels using a hybrid manual-automatic approach that leverages Wikipedia links from the AIDA-CoNLL-YAGO dataset. The text base comes from the Reiss et al. (2020) corrected version of CoNLL-03.

### Format

Five columns per token (space-separated), BIO tagging:

| Column | Description |
|--------|-------------|
| Token | Word text |
| POS | Part-of-speech tag |
| Wikipedia | Named Entity Linking annotation (Wikipedia reference) |
| NER (CleanCoNLL*) | NER tags before Phase 3 correction |
| NER (CleanCoNLL) | Final corrected NER labels |

Entity types: PER, LOC, ORG, MISC. Sentences separated by empty lines; articles delimited by `-DOCSTART-` tokens.

### Source

[flairNLP/CleanCoNLL](https://github.com/flairNLP/CleanCoNLL) on GitHub. The repository distributes masked annotations (`[TOKEN]` placeholders) and patch files — the original Reuters text requires reconstruction from the CoNLL-03 corpus.

Paper: [CleanCoNLL: A Nearly Noise-Free Named Entity Recognition Dataset](https://aclanthology.org/2023.emnlp-main.533/) (EMNLP 2023).

### Getting the Data

The data files are **not** included in this repo (they require the Reuters corpus). To reconstruct:

```bash
cd benchmarks/cleanconll
python download_dataset.py
```

This clones the upstream repo, runs their reconstruction script (which downloads CoNLL-03 and applies patches), and copies the output files into `data/`.

Requires: `git`, `bash`, `wget`.

## Entity Resolution

`resolve_entities.py` resolves Wikipedia page titles from CleanCoNLL annotations to Wikidata QIDs with P31/P279 type information. It first checks the local classified parquet (`data_dump_core_output/classified_with_sitelinks.parquet`, covers ~96%), then falls back to the Wikidata API for the rest, including Wikipedia redirect resolution.

`classified_with_sitelinks.parquet` is expected to come from the HuggingFace release artifacts, not from the default classifier output generated locally in this repo.

```bash
python resolve_entities.py           # full run (parquet + API)
python resolve_entities.py --no-fetch  # parquet-only, no API calls
```

**Results**: 5,937 / 5,941 resolved (99.93%). 4 unresolved are deleted Wikipedia articles:
- `Andean_states`
- `Eisuke_Sakakibara`
- `National_League_Western`
- `Republican_Stadium_(Chişinău)`

**Note**: CleanCoNLL data contains a few titles with `\x02` control character prefix (data artifact), which the script strips automatically.

## Classification Evaluation

`eval_classification.py` classifies all CleanCoNLL entities using the YAML gate definitions and compares against NER tags. Generates `classification_report.md` with confusion matrix, misalignment analysis by pattern, and unclassified P31 types.

```bash
uv run --project src/python python benchmarks/cleanconll/eval_classification.py          # print summary
uv run --project src/python python benchmarks/cleanconll/eval_classification.py --report  # also generate report
```

## Files

| File | Purpose |
|------|---------|
| `data/cleanconll.{train,dev,test}` | Reconstructed CleanCoNLL splits (gitignored) |
| `data/cleanconll_entities.tsv` | Resolved entities: wikipedia_title, qid, label, description, p31/p279 QIDs and labels |
| `download_dataset.py` | Clones repo, runs reconstruction, copies output |
| `resolve_entities.py` | Resolves Wikipedia titles → Wikidata QIDs with P31/P279 |
| `eval_classification.py` | Evaluates YAML classification against NER tags, generates report |
| `classification_report.md` | Auto-generated evaluation report (re-run `--report` to update) |
