"""
Parse Voyage rerank batch output into the same TSV format as run_extend.py.

Reads rerank_batch_output.jsonl + rerank_metadata.json, picks the
highest-scoring candidate per sample, writes result TSV.
"""

import json
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BATCH_DIR = BASE_DIR / "batch"

parser = argparse.ArgumentParser()
parser.add_argument(
    '-output_files',
    type=str,
    nargs='+',
    default=[
        str(BATCH_DIR / 'wikidata_claims_rerank_output_0.jsonl'),
        str(BATCH_DIR / 'wikidata_claims_rerank_output_1.jsonl'),
    ],
)
parser.add_argument('-metadata_file', type=str, default=str(BATCH_DIR / 'wikidata_claims_rerank_metadata.json'))
parser.add_argument('-result_file', type=str, default=str(DATA_DIR / 'wikidata_claims_rerank.result'))


def main():
    args = parser.parse_args()

    # Load metadata
    with open(args.metadata_file) as f:
        metadata_list = json.load(f)
    metadata = {m["custom_id"]: m for m in metadata_list}

    # Load rerank results from all output files
    results = []
    for path in args.output_files:
        with open(path) as f:
            results.extend(json.loads(line) for line in f if line.strip())

    with open(args.result_file, 'w', encoding='utf8') as wf:
        for r in sorted(results, key=lambda x: x["custom_id"]):
            custom_id = r["custom_id"]
            meta = metadata[custom_id]

            error = r.get("error")
            if error:
                print(f"  {custom_id}: ERROR - {error}")
                continue

            data = r["response"]["body"]["data"]
            # Pick candidate with highest relevance_score
            best = max(data, key=lambda x: x["relevance_score"])
            predicted = meta["candidates"][best["index"]]

            wf.write(f"{meta['id']}\t{meta['mention']}\t{predicted}\t{meta['gold_label']}\n")

    print(f"Written {args.result_file}")


if __name__ == '__main__':
    main()
