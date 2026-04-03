"""Print the BLINK controlled-subset table reported in the paper."""

from __future__ import annotations

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def extract_predicted_name(text: str):
    match = re.search(r'\[(.*?)\]', text)
    if match:
        return match.group(1)
    return None


def chunk_by_can_num(mentions, candidate_file, interval=(5, 10, 20)):
    min_num, middle_num, max_num = interval
    candidate_num = {}
    for line in open(candidate_file, encoding='utf8'):
        row = line.strip().split('\t')
        mention, candidates = row[0], row[1:]
        candidate_num[mention] = len(candidates)

    groups = [[], [], [], []]
    for mention in mentions:
        cnt = candidate_num[mention]
        if cnt < min_num:
            groups[0].append(mention)
        elif cnt < middle_num:
            groups[1].append(mention)
        elif cnt < max_num:
            groups[2].append(mention)
        else:
            groups[3].append(mention)
    return groups


def evaluate(result_file: Path, candidate_file: Path):
    rows = [line.strip().split('\t') for line in open(result_file, encoding='utf8')][:19000]
    mentions = [row[1] for row in rows]
    groups = chunk_by_can_num(mentions, candidate_file)

    accs = []
    counts = []
    for group in groups:
        correct = 0
        total = 0
        group_set = set(group)
        for row in rows:
            mention, predicted, label = row[1], row[2], row[3]
            if mention not in group_set:
                continue
            if extract_predicted_name(predicted):
                predicted = predicted.split(' [')[0]
            if predicted == label:
                correct += 1
            total += 1
        counts.append(total)
        accs.append(correct / total)
    return counts, accs


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def main():
    yago_counts, yago_accs = evaluate(DATA_DIR / "yago_new_rerank.result", DATA_DIR / "yago_new.candidate")
    module_counts, module_accs = evaluate(DATA_DIR / "wikidata_claims_rerank.result", DATA_DIR / "wikidata_new.candidate")

    assert yago_counts == module_counts

    print("| Group | Candidates | Mentions | YAGO-4.5 types | Module-based types |")
    print("|---|---:|---:|---:|---:|")
    labels = ["<5", "5-9", "10-19", ">=20"]
    for i, label in enumerate(labels):
        print(f"| {i+1} | {label} | {yago_counts[i]:,} | {pct(yago_accs[i])} | {pct(module_accs[i])} |")
    print(f"| **Macro Avg** |  | **{sum(yago_counts):,}** | **{pct(sum(yago_accs)/4)}** | **{pct(sum(module_accs)/4)}** |")


if __name__ == "__main__":
    main()
