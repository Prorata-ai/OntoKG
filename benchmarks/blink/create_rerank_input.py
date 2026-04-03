"""
Create JSONL batch input for Voyage AI rerank-2.5 entity disambiguation.

Reads blink_test.json + candidate file, produces:
  - rerank_batch_input_*.jsonl  (for Voyage batch API, split by 10MB limit)
  - rerank_metadata.json        (sidecar for parsing results back to eval format)
"""

import json
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BATCH_DIR = BASE_DIR / "batch"

parser = argparse.ArgumentParser()
parser.add_argument('-dataset_file', type=str, default=str(DATA_DIR / 'blink_test.json'))
parser.add_argument('-candidate_file', type=str, default=str(DATA_DIR / 'yago_new.candidate'))
parser.add_argument('-output_dir', type=str, default=str(BATCH_DIR))
parser.add_argument('-prefix', type=str, default='rerank', help='Prefix for output filenames')
parser.add_argument('-limit', type=int, default=0, help='Limit number of samples (0 = all)')
parser.add_argument('-max_file_size_mb', type=float, default=8, help='Max file size in MB (Voyage limit is 10MB)')

INSTRUCTION = "Identify which entity the mention between [START_ENT] and [END_ENT] refers to."


def load_candidates(path):
    candidates = {}
    for line in open(path, encoding='utf8'):
        row = line.strip().split('\t')
        mention, cands = row[0], row[1:]
        candidates[mention] = cands
    return candidates


def main():
    args = parser.parse_args()
    max_file_bytes = int(args.max_file_size_mb * 1024 * 1024)
    import os
    os.makedirs(args.output_dir, exist_ok=True)
    output_prefix = os.path.join(args.output_dir, f"{args.prefix}_input")
    metadata_file = os.path.join(args.output_dir, f"{args.prefix}_metadata.json")

    candidates = load_candidates(args.candidate_file)

    samples = [json.loads(line) for line in open(args.dataset_file, encoding='utf8')]
    if args.limit > 0:
        samples = samples[:args.limit]

    metadata = []
    file_index = 0
    current_size = 0
    current_file = None

    def open_next_file():
        nonlocal file_index, current_size, current_file
        if current_file:
            current_file.close()
        path = f"{output_prefix}_{file_index}.jsonl"
        current_file = open(path, 'w')
        current_size = 0
        file_index += 1
        return path

    files_created = []
    files_created.append(open_next_file())

    for i, sample in enumerate(samples):
        sample_id = sample['id']
        input_sentence = sample['input']
        mention = sample['meta']['mention']
        gold_label = sample['output'][0]['provenance'][0]['title']

        if mention not in candidates:
            continue

        cands = candidates[mention]
        if not cands:
            continue

        query = f"{INSTRUCTION}\n\n{input_sentence}"

        record = {
            "custom_id": f"rerank_{i}",
            "body": {
                "query": query,
                "documents": cands,
            },
        }
        line = json.dumps(record) + "\n"
        line_bytes = len(line.encode('utf8'))

        # Split to new file if this line would exceed limit
        if current_size + line_bytes > max_file_bytes and current_size > 0:
            files_created.append(open_next_file())

        current_file.write(line)
        current_size += line_bytes

        metadata.append({
            "custom_id": f"rerank_{i}",
            "id": sample_id,
            "mention": mention,
            "gold_label": gold_label,
            "candidates": cands,
        })

    if current_file:
        current_file.close()

    with open(metadata_file, 'w') as f:
        json.dump(metadata, f)

    print(f"Created {len(files_created)} input file(s):")
    for path in files_created:
        import os
        size_mb = os.path.getsize(path) / (1024 * 1024)
        lines = sum(1 for _ in open(path))
        print(f"  {path}: {lines} requests, {size_mb:.1f} MB")
    print(f"Created {metadata_file} with {len(metadata)} metadata entries.")


if __name__ == '__main__':
    main()
