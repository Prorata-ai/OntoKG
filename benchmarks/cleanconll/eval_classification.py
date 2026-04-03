"""Evaluate YAML category classification against CleanCoNLL NER tags.

Classifies all entities using P31 values from cleanconll_entities.tsv against
the YAML gate definitions, then compares with CleanCoNLL NER tags (PER, LOC, ORG, MISC).
Generates classification_report.md with full misalignment analysis.

Usage:
    python eval_classification.py [--report]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
PROJECT_ROOT = SCRIPT_DIR.parent.parent
TAGS_DIR = PROJECT_ROOT / "src" / "tags"

SPLITS = ["train", "dev", "test"]

NER_TO_CATEGORIES = {
    "PER": ["people"],
    "LOC": ["places"],
    "ORG": ["organizations"],
    "MISC": [
        "creative_works_media", "events_actions",
        "knowledge_information_abstracts", "products_artifacts",
        "science", "organizations", "places", "people",
    ],
}

CATEGORY_SHORT = {
    "creative_works_media": "creative_works",
    "events_actions": "events_actions",
    "knowledge_information_abstracts": "knowledge",
    "organizations": "organizations",
    "people": "people",
    "places": "places",
    "products_artifacts": "products",
    "science": "science",
}


def load_gate_qids() -> dict[str, str]:
    qid_to_cat: dict[str, str] = {}
    for path in sorted(TAGS_DIR.glob("*.yaml")):
        with open(path) as f:
            data = yaml.safe_load(f)
        cat = path.stem
        for qid in data.get("gate_qids", []):
            qid_to_cat[str(qid)] = cat
    return qid_to_cat


def load_resolved_entities() -> dict[str, dict]:
    entities = {}
    tsv_path = DATA_DIR / "cleanconll_entities.tsv"
    with open(tsv_path) as f:
        next(f)
        for line in f:
            cols = line.rstrip("\n").split("\t")
            title = cols[0]
            qid = cols[1]
            p31 = cols[4].split(";") if cols[4] else []
            p31_labels = cols[5].split(";") if cols[5] else []
            p279 = cols[6].split(";") if cols[6] else []
            if qid:
                entities[title] = {
                    "qid": qid, "p31": p31, "p31_labels": p31_labels, "p279": p279,
                }
    return entities


def classify_entity(p31: list[str], p279: list[str], qid_to_cat: dict[str, str]) -> str | None:
    for qid in p31:
        if qid in qid_to_cat:
            return qid_to_cat[qid]
    for qid in p279:
        if qid in qid_to_cat:
            return qid_to_cat[qid]
    return None


def extract_entities(split: str) -> dict[str, str]:
    """Extract entities from CleanCoNLL split. Returns {title: ner_tag}."""
    entities: dict[str, str] = {}
    path = DATA_DIR / f"cleanconll.{split}"
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("-DOCSTART-"):
                continue
            cols = line.split("\t")
            if len(cols) < 5:
                continue
            wiki = cols[2]
            ner = cols[4]
            if wiki != "O" and ner != "O":
                title = wiki[2:] if wiki[:2] in ("B-", "I-") else wiki
                title = title.lstrip("\x00\x01\x02\x03\x04\x05\x06\x07\x08")
                tag = ner[2:] if ner[:2] in ("B-", "I-") else ner
                if title not in entities:
                    entities[title] = tag
    return entities


def evaluate_split(split: str, resolved: dict, qid_to_cat: dict) -> list[tuple]:
    """Classify entities for one split. Returns [(title, ner, category, status, split)]."""
    entities = extract_entities(split)
    results = []
    for title, ner_tag in sorted(entities.items()):
        ent = resolved.get(title)
        if not ent:
            results.append((title, ner_tag, None, "no_qid", split))
            continue
        category = classify_entity(ent["p31"], ent["p279"], qid_to_cat)
        if category is None:
            results.append((title, ner_tag, None, "unclassified", split))
        else:
            results.append((title, ner_tag, category, "classified", split))
    return results


def compute_stats(results: list[tuple]) -> dict:
    classified = [(t, n, c) for t, n, c, s, _ in results if s == "classified"]
    unclassified = [(t, n) for t, n, c, s, _ in results if s == "unclassified"]
    no_qid = [(t, n) for t, n, c, s, _ in results if s == "no_qid"]

    correct = wrong = 0
    matrix: dict[str, Counter] = {t: Counter() for t in ["PER", "LOC", "ORG", "MISC"]}
    misaligned = []

    for title, ner_tag, category in classified:
        matrix[ner_tag][category] += 1
        expected = NER_TO_CATEGORIES.get(ner_tag, [])
        if category in expected:
            correct += 1
        else:
            wrong += 1
            misaligned.append((title, ner_tag, category))

    return {
        "total": len(results),
        "classified": len(classified),
        "unclassified": len(unclassified),
        "no_qid": len(no_qid),
        "aligned": correct,
        "misaligned_count": wrong,
        "matrix": matrix,
        "misaligned": misaligned,
        "unclassified_list": unclassified,
    }


def p31_display(ent: dict) -> str:
    pairs = []
    for q, l in zip(ent.get("p31", []), ent.get("p31_labels", [])):
        pairs.append(f"{l}" if l else q)
    return "; ".join(pairs) if pairs else ""


def generate_report(all_results: list[tuple], per_split: dict[str, dict],
                    agg: dict, resolved: dict) -> str:
    lines = []
    w = lines.append

    w("# CleanCoNLL Classification Evaluation Report")
    w("")
    w("Evaluation of the YAML-based Wikidata category classification system against CleanCoNLL NER annotations.")
    w("Generated by `eval_classification.py` — re-run to update after YAML gate changes.")
    w("")

    # --- Classification Rate ---
    w("## Classification Rate")
    w("")
    w("| Split | Entities | Classified | Rate | Unclassified | No QID |")
    w("|-------|:--------:|:----------:|:----:|:------------:|:------:|")
    for sp in SPLITS:
        s = per_split[sp]
        rate = 100 * s["classified"] / s["total"]
        unclass_pct = 100 * s["unclassified"] / s["total"]
        w(f"| {sp} | {s['total']:,} | {s['classified']:,} | {rate:.1f}% "
          f"| {s['unclassified']:,} ({unclass_pct:.1f}%) | {s['no_qid']} |")
    rate = 100 * agg["classified"] / agg["total"]
    unclass_pct = 100 * agg["unclassified"] / agg["total"]
    w(f"| **All** | **{agg['total']:,}** | **{agg['classified']:,}** | **{rate:.1f}%** "
      f"| **{agg['unclassified']:,} ({unclass_pct:.1f}%)** | **{agg['no_qid']}** |")
    w("")

    # --- Alignment Rate ---
    w("## NER Alignment Rate")
    w("")
    w("| Split | Classified | Aligned | Rate | Misaligned |")
    w("|-------|:----------:|:-------:|:----:|:----------:|")
    for sp in SPLITS:
        s = per_split[sp]
        rate = 100 * s["aligned"] / s["classified"] if s["classified"] else 0
        w(f"| {sp} | {s['classified']:,} | {s['aligned']:,} | {rate:.1f}% | {s['misaligned_count']} |")
    rate = 100 * agg["aligned"] / agg["classified"]
    w(f"| **All** | **{agg['classified']:,}** | **{agg['aligned']:,}** | **{rate:.1f}%** | **{agg['misaligned_count']}** |")
    w("")

    # --- Confusion Matrix ---
    all_cats = sorted(CATEGORY_SHORT.keys())
    w("## Confusion Matrix (All Splits)")
    w("")
    header = "| NER \\\\ Category | " + " | ".join(CATEGORY_SHORT[c] for c in all_cats) + " | Total |"
    w(header)
    w("|:-:|" + ":-:|" * (len(all_cats) + 1))
    matrix = agg["matrix"]
    for ner_tag in ["PER", "LOC", "ORG", "MISC"]:
        row_total = sum(matrix[ner_tag][c] for c in all_cats)
        cells = " | ".join(f"{matrix[ner_tag][c]:,}" for c in all_cats)
        w(f"| **{ner_tag}** | {cells} | {row_total:,} |")
    w("")

    # --- Misalignment Analysis ---
    misaligned_entries = agg["misaligned"]
    w("## Misalignment Analysis")
    w("")
    w(f"{agg['misaligned_count']} misaligned entities across all splits.")
    w("")

    # Group by pattern
    patterns: dict[str, list] = {}
    for title, ner, cat in misaligned_entries:
        ent = resolved.get(title, {})
        p31_str = p31_display(ent)
        key = f"{ner}→{CATEGORY_SHORT.get(cat, cat)}"
        patterns.setdefault(key, []).append((title, ner, cat, p31_str))

    sorted_patterns = sorted(patterns.items(), key=lambda x: -len(x[1]))

    w("### By Pattern")
    w("")
    w("| Pattern | Count | % | Examples |")
    w("|---------|:-----:|:-:|---------|")
    for pattern, entries in sorted_patterns:
        pct = 100 * len(entries) / agg["misaligned_count"]
        examples = ", ".join(e[0].replace("_", " ") for e in entries[:3])
        if len(entries) > 3:
            examples += f", … (+{len(entries) - 3})"
        w(f"| {pattern} | {len(entries)} | {pct:.1f}% | {examples} |")
    w("")

    # Detailed breakdown of larger patterns
    for pattern, entries in sorted_patterns:
        if len(entries) < 5:
            continue
        w(f"### {pattern} — {len(entries)} entities")
        w("")

        by_p31: dict[str, list] = {}
        for title, ner, cat, p31_str in entries:
            ent = resolved.get(title, {})
            labels = ent.get("p31_labels", [])
            group = labels[0] if labels else "unknown"
            by_p31.setdefault(group, []).append((title, p31_str))

        by_p31_sorted = sorted(by_p31.items(), key=lambda x: -len(x[1]))

        w("| P31 Type | Count | Examples |")
        w("|----------|:-----:|---------|")
        for p31_type, items in by_p31_sorted:
            examples = ", ".join(t.replace("_", " ") for t, _ in items[:3])
            if len(items) > 3:
                examples += f", … (+{len(items) - 3})"
            w(f"| {p31_type} | {len(items)} | {examples} |")
        w("")

    # --- Unclassified Analysis ---
    unclassified = agg["unclassified_list"]
    w("## Unclassified Analysis")
    w("")
    w(f"{agg['unclassified']} entities ({100 * agg['unclassified'] / agg['total']:.1f}%) "
      f"have P31 types not covered by any YAML gate definition.")
    w("")

    unclass_by_ner = Counter(n for _, n in unclassified)
    w("### By NER Tag")
    w("")
    w("| NER | Count | % of unclassified |")
    w("|:---:|:-----:|:-:|")
    for tag, count in unclass_by_ner.most_common():
        pct = 100 * count / len(unclassified) if unclassified else 0
        w(f"| {tag} | {count} | {pct:.1f}% |")
    w("")

    p31_counter: Counter = Counter()
    p31_to_label: dict[str, str] = {}
    p31_to_ner: dict[str, Counter] = {}
    for title, ner in unclassified:
        ent = resolved.get(title, {})
        for qid, label in zip(ent.get("p31", []), ent.get("p31_labels", [])):
            p31_counter[qid] += 1
            p31_to_label[qid] = label
            p31_to_ner.setdefault(qid, Counter())[ner] += 1

    w("### Top Unclassified P31 Types")
    w("")
    w("| Count | P31 QID | Label | Dominant NER |")
    w("|:-----:|---------|-------|:---:|")
    for qid, count in p31_counter.most_common(15):
        label = p31_to_label.get(qid, "")
        dom_ner = p31_to_ner[qid].most_common(1)[0][0]
        w(f"| {count} | {qid} | {label} | {dom_ner} |")
    w("")

    # --- Summary ---
    w("## Summary")
    w("")
    cls_rate = 100 * agg["classified"] / agg["total"]
    align_rate = 100 * agg["aligned"] / agg["classified"]
    overall_rate = 100 * agg["aligned"] / agg["total"]
    w("| Metric | Value |")
    w("|--------|------:|")
    w(f"| Classification rate | {cls_rate:.1f}% ({agg['classified']:,} / {agg['total']:,}) |")
    w(f"| NER alignment rate | {align_rate:.1f}% ({agg['aligned']:,} / {agg['classified']:,}) |")
    w(f"| Overall accuracy (classified + aligned) | {overall_rate:.1f}% ({agg['aligned']:,} / {agg['total']:,}) |")
    w("")

    return "\n".join(lines)


def main(write_report: bool = False) -> None:
    print(f"Loading YAML gate definitions from {TAGS_DIR} …")
    qid_to_cat = load_gate_qids()
    print(f"  {len(qid_to_cat):,} gate QIDs across 8 categories")

    print("\nLoading resolved entities …")
    resolved = load_resolved_entities()
    print(f"  {len(resolved):,} resolved entities")

    all_results = []
    per_split = {}

    for sp in SPLITS:
        results = evaluate_split(sp, resolved, qid_to_cat)
        all_results.extend(results)
        per_split[sp] = compute_stats(results)
        n = per_split[sp]
        print(f"\n  {sp}: {n['total']:,} entities, {n['classified']:,} classified "
              f"({100*n['classified']/n['total']:.1f}%), "
              f"{n['aligned']}/{n['classified']} aligned "
              f"({100*n['aligned']/n['classified']:.1f}%)")

    agg = compute_stats(all_results)

    print(f"\n{'='*70}")
    print("AGGREGATE (all splits)")
    print(f"{'='*70}")
    print(f"  Total: {agg['total']:,}")
    print(f"  Classified: {agg['classified']:,} ({100*agg['classified']/agg['total']:.1f}%)")
    print(f"  Aligned: {agg['aligned']:,}/{agg['classified']:,} ({100*agg['aligned']/agg['classified']:.1f}%)")
    print(f"  Overall: {agg['aligned']:,}/{agg['total']:,} ({100*agg['aligned']/agg['total']:.1f}%)")

    if write_report:
        report = generate_report(all_results, per_split, agg, resolved)
        report_path = SCRIPT_DIR / "classification_report.md"
        report_path.write_text(report)
        print(f"\n  Report written to {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="Generate classification_report.md")
    args = parser.parse_args()
    main(write_report=args.report)
