# BLINK Benchmark

This folder contains the controlled-subset BLINK entity disambiguation artifact used for the paper's comparison between YAGO-4.5 types and OntoKG module-based types.

For this experiment, the official YAGO 4.5 evaluation repo is `https://github.com/tigerchen52/eval_yago_el`.

## Scope

This is the **controlled subset** experiment, not the full 19,661-mention BLINK evaluation.

- Subset size: **12,323 mentions**
- Disambiguation model: **Voyage rerank-2.5**
- Comparison: **YAGO-4.5 candidate types** vs **OntoKG module-based candidate types**

The included files are enough to reproduce the paper table **without calling Voyage again**.

## Included Files

### Data

- `data/blink_test.json`
  Controlled BLINK subset input samples.
- `data/yago_new.candidate`
  YAGO-4.5 candidate list for the subset.
- `data/wikidata_new.candidate`
  OntoKG module-based candidate list for the subset.
- `data/yago_new_rerank.result`
  Precomputed Voyage rerank results for the YAGO-4.5 candidates.
- `data/wikidata_claims_rerank.result`
  Precomputed Voyage rerank results for the OntoKG module-based candidates.

### Batch Outputs

- `batch/yago_new_rerank_metadata.json`
- `batch/yago_new_rerank_input_0.jsonl`
- `batch/yago_new_rerank_input_1.jsonl`
- `batch/yago_new_rerank_output_0.jsonl`
- `batch/yago_new_rerank_output_1.jsonl`
- `batch/wikidata_claims_rerank_metadata.json`
- `batch/wikidata_claims_rerank_input_0.jsonl`
- `batch/wikidata_claims_rerank_input_1.jsonl`
- `batch/wikidata_claims_rerank_output_0.jsonl`
- `batch/wikidata_claims_rerank_output_1.jsonl`

These are the preserved Voyage batch artifacts for both sides of the controlled comparison. The module-based outputs can be re-parsed locally with `parse_rerank_output.py`, and the YAGO batch files are included for completeness and inspection.

### Python Scripts

- `eval_result.py`
  Computes accuracy by candidate-count bucket and macro average.
  This evaluation script is adapted from `https://github.com/tigerchen52/eval_yago_el`.
- `create_rerank_input.py`
  Creates Voyage rerank JSONL input from the included subset and candidate file.
- `parse_rerank_output.py`
  Converts downloaded Voyage batch output JSONL into the `.result` TSV format.
- `reproduce_paper_result.py`
  Prints the paper table directly from the included result files.

## Quick Reproduction

Run from the repo root:

```bash
python3 benchmarks/blink/reproduce_paper_result.py
```

Expected output:

```text
| Group | Candidates | Mentions | YAGO-4.5 types | Module-based types |
|---|---:|---:|---:|---:|
| 1 | <5 | 10,284 | 91.2% | 91.6% |
| 2 | 5-9 | 1,666 | 84.4% | 85.4% |
| 3 | 10-19 | 348 | 82.5% | 82.8% |
| 4 | >=20 | 25 | 68.0% | 76.0% |
| **Macro Avg** |  | **12,323** | **81.5%** | **83.9%** |
```

You can also evaluate each side directly:

```bash
python3 benchmarks/blink/eval_result.py \
  -result_file benchmarks/blink/data/yago_new_rerank.result \
  -candidate_file benchmarks/blink/data/yago_new.candidate

python3 benchmarks/blink/eval_result.py \
  -result_file benchmarks/blink/data/wikidata_claims_rerank.result \
  -candidate_file benchmarks/blink/data/wikidata_new.candidate
```

## Notes

- No Voyage call is needed for paper-result reproduction because the rerank outputs and parsed result files are already included.
- `parse_rerank_output.py` defaults to the included module-based batch output files in `batch/`.
- The broader candidate-generation workflow described in the original `eval_yago_el` repo is not fully repackaged here; this folder is focused on preserving the paper artifact needed for the reported BLINK result.
