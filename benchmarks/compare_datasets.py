"""Three-way comparison: AIDA-YAGO vs CleanCoNLL vs YAML classification.

Focuses on the shared entity subset between AIDA and CleanCoNLL.
Analyzes three-way agreement, detects annotation errors in both datasets,
and identifies NER-vs-ontology boundary cases.
Generates classification_comparison_report.md.

Usage:
    python compare_datasets.py [--report]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
TAGS_DIR = PROJECT_ROOT / "src" / "tags"
AIDA_DIR = SCRIPT_DIR / "aida_yago" / "data"
CLEAN_DIR = SCRIPT_DIR / "cleanconll" / "data"

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


def classify_entity(p31: list[str], p279: list[str], qid_to_cat: dict[str, str]) -> str | None:
    for qid in p31:
        if qid in qid_to_cat:
            return qid_to_cat[qid]
    for qid in p279:
        if qid in qid_to_cat:
            return qid_to_cat[qid]
    return None


def is_aligned(ner_tag: str, category: str) -> bool:
    return category in NER_TO_CATEGORIES.get(ner_tag, [])


def load_resolved(tsv_path: Path) -> dict[str, dict]:
    entities = {}
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


# Tie-break order: PER > ORG > LOC > MISC (specificity, most constrained first)
_TAG_PRIORITY = {"PER": 0, "ORG": 1, "LOC": 2, "MISC": 3}


def majority_tag(counter: Counter) -> str:
    """Return the most common NER tag. Ties broken by tag priority (PER > ORG > LOC > MISC)."""
    top_count = counter.most_common(1)[0][1]
    tied = [tag for tag, cnt in counter.items() if cnt == top_count]
    tied.sort(key=lambda t: _TAG_PRIORITY.get(t, 99))
    return tied[0]


def extract_aida_entities() -> dict[str, str]:
    """Extract all AIDA entities (all splits). Returns {title: ner_tag}.

    Uses majority NER tag across all mentions of the same title.
    """
    tag_counts: dict[str, Counter] = {}
    with open(AIDA_DIR / "aida_conll_merged.tsv") as f:
        next(f)
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 9:
                continue
            ner = cols[5]
            url = cols[8].strip()
            if not url or "/wiki/" not in url:
                continue
            title = url.rsplit("/wiki/", 1)[-1]
            if not title:
                continue
            tag = ner[2:] if ner[:2] in ("B-", "I-") else ner
            if tag == "O":
                continue
            tag_counts.setdefault(title, Counter())[tag] += 1
    return {title: majority_tag(c) for title, c in tag_counts.items()}


def extract_clean_entities() -> dict[str, str]:
    """Extract all CleanCoNLL entities (all splits). Returns {title: ner_tag}.

    Uses majority NER tag across all mentions of the same title.
    """
    tag_counts: dict[str, Counter] = {}
    for split in ["train", "dev", "test"]:
        path = CLEAN_DIR / f"cleanconll.{split}"
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
                    tag_counts.setdefault(title, Counter())[tag] += 1
    return {title: majority_tag(c) for title, c in tag_counts.items()}


def p31_display(ent: dict) -> str:
    pairs = []
    for q, l in zip(ent.get("p31", []), ent.get("p31_labels", [])):
        pairs.append(f"{l}" if l else q)
    return "; ".join(pairs) if pairs else ""


def cat_short(cat: str) -> str:
    return CATEGORY_SHORT.get(cat, cat)


def generate_report(shared: list[dict], aida_qid_count: int, clean_qid_count: int,
                    resolved: dict) -> str:
    lines = []
    w = lines.append

    w("# Three-Way Classification Comparison: AIDA-YAGO vs CleanCoNLL vs YAML System")
    w("")
    w("Cross-dataset evaluation on the shared entity subset (matched by Wikidata QID) between AIDA-YAGO and CleanCoNLL.")
    w("Generated by `compare_datasets.py` — re-run to update after YAML gate changes.")
    w("")

    # --- Dataset sizes ---
    w("## Datasets")
    w("")
    w("| | AIDA-YAGO | CleanCoNLL | Shared |")
    w("|---|:-:|:-:|:-:|")
    w(f"| Unique entities (by QID) | {aida_qid_count:,} | {clean_qid_count:,} | {len(shared):,} |")
    w("")

    # --- Classify shared entities ---
    classified = [e for e in shared if e["category"] is not None]
    unclassified = [e for e in shared if e["category"] is None]

    w("## Classification on Shared Subset")
    w("")
    w("| Metric | Count |")
    w("|--------|------:|")
    w(f"| Shared entities | {len(shared):,} |")
    w(f"| Classified | {len(classified):,} ({100*len(classified)/len(shared):.1f}%) |")
    w(f"| Unclassified | {len(unclassified):,} ({100*len(unclassified)/len(shared):.1f}%) |")
    w("")

    # --- Three-way agreement ---
    all_agree = []
    aida_wrong_clean_ours_agree = []
    clean_wrong_aida_ours_agree = []
    both_ner_wrong_ours_right = []
    all_disagree = []

    for e in classified:
        aida_ok = is_aligned(e["aida_ner"], e["category"])
        clean_ok = is_aligned(e["clean_ner"], e["category"])

        if aida_ok and clean_ok:
            all_agree.append(e)
        elif not aida_ok and clean_ok:
            aida_wrong_clean_ours_agree.append(e)
        elif aida_ok and not clean_ok:
            clean_wrong_aida_ours_agree.append(e)
        else:
            # Both NER disagree with ours
            if e["aida_ner"] == e["clean_ner"]:
                both_ner_wrong_ours_right.append(e)
            else:
                all_disagree.append(e)

    w("## Three-Way Agreement Analysis")
    w("")
    w(f"On the {len(shared):,} shared entities, {len(classified):,} classified:")
    w("")
    w("| Scenario | Count | Interpretation |")
    w("|----------|:-----:|----------------|")
    w(f"| All three agree | {len(all_agree):,} | Consensus — high confidence |")
    w(f"| AIDA wrong, CleanCoNLL + Ours agree | {len(aida_wrong_clean_ours_agree)} | AIDA annotation error, independently confirmed |")
    w(f"| CleanCoNLL wrong, AIDA + Ours agree | {len(clean_wrong_aida_ours_agree)} | CleanCoNLL annotation error, independently confirmed |")
    w(f"| Both NER differ from ours | {len(both_ner_wrong_ours_right)} | Entity linking errors, disambiguation pages, or NER-vs-ontology boundary |")
    w(f"| All three disagree | {len(all_disagree)} | Both NER systems and ours all disagree |")
    w(f"| Unclassified | {len(unclassified)} | No gate match — cannot evaluate |")
    w("")

    # --- Where AIDA and CleanCoNLL disagree ---
    aida_clean_disagree = [e for e in classified if e["aida_ner"] != e["clean_ner"]]
    if aida_clean_disagree:
        agrees_clean = sum(1 for e in aida_clean_disagree if is_aligned(e["clean_ner"], e["category"]) and not is_aligned(e["aida_ner"], e["category"]))
        agrees_aida = sum(1 for e in aida_clean_disagree if is_aligned(e["aida_ner"], e["category"]) and not is_aligned(e["clean_ner"], e["category"]))
        agrees_neither = len(aida_clean_disagree) - agrees_clean - agrees_aida
        # Count where both are aligned (possible when MISC maps to everything)
        both_aligned = sum(1 for e in aida_clean_disagree if is_aligned(e["aida_ner"], e["category"]) and is_aligned(e["clean_ner"], e["category"]))
        agrees_neither -= both_aligned

        w("### Where AIDA and CleanCoNLL Disagree")
        w("")
        w(f"The two NER datasets disagree on **{len(aida_clean_disagree)} entities**. Our system serves as a tiebreaker:")
        w("")
        w("| Our system agrees with | Count |")
        w("|------------------------|:-----:|")
        w(f"| CleanCoNLL | {agrees_clean} |")
        w(f"| AIDA | {agrees_aida} |")
        if both_aligned:
            w(f"| Both (MISC flexibility) | {both_aligned} |")
        w(f"| Neither | {agrees_neither} |")
        w("")
        if agrees_aida > 0:
            ratio = agrees_clean / agrees_aida
            w(f"Our system agrees with CleanCoNLL at a **{ratio:.1f}:1 ratio** over AIDA when the two NER systems disagree.")
            w("")

    # --- AIDA errors detected ---
    w("## AIDA Annotation Errors Detected")
    w("")
    w(f"The YAML system identifies **{len(aida_wrong_clean_ours_agree)} entities** where AIDA is wrong and CleanCoNLL + our system agree:")
    w("")

    aida_err_patterns: dict[str, list] = {}
    for e in aida_wrong_clean_ours_agree:
        key = f"{e['aida_ner']}→{cat_short(e['category'])}"
        aida_err_patterns.setdefault(key, []).append(e)

    w("| Error Pattern | Count | Examples |")
    w("|---------------|:-----:|---------|")
    for pattern, entries in sorted(aida_err_patterns.items(), key=lambda x: -len(x[1])):
        examples = ", ".join(e["title"].replace("_", " ") for e in entries[:3])
        if len(entries) > 3:
            examples += f", … (+{len(entries) - 3})"
        w(f"| {pattern} | {len(entries)} | {examples} |")
    w("")

    # Detailed top AIDA error patterns
    for pattern, entries in sorted(aida_err_patterns.items(), key=lambda x: -len(x[1])):
        if len(entries) < 5:
            continue
        w(f"### {pattern} — {len(entries)} entities")
        w("")
        w("| Entity | AIDA | CleanCoNLL | Ours | P31 |")
        w("|--------|:----:|:----------:|:----:|-----|")
        for e in entries[:15]:
            ent = resolved.get(e["title"], {})
            p31 = p31_display(ent)
            w(f"| {e['title'].replace('_', ' ')} | {e['aida_ner']} | {e['clean_ner']} | {cat_short(e['category'])} | {p31[:60]} |")
        if len(entries) > 15:
            w(f"| … +{len(entries) - 15} more | | | | |")
        w("")

    # --- CleanCoNLL errors detected ---
    w("## CleanCoNLL Errors Detected")
    w("")
    w(f"**{len(clean_wrong_aida_ours_agree)} entities** where CleanCoNLL is wrong and AIDA + our system agree:")
    w("")

    clean_err_patterns: dict[str, list] = {}
    for e in clean_wrong_aida_ours_agree:
        key = f"{e['clean_ner']}→{cat_short(e['category'])}"
        clean_err_patterns.setdefault(key, []).append(e)

    w("| Error Pattern | Count | Examples |")
    w("|---------------|:-----:|---------|")
    for pattern, entries in sorted(clean_err_patterns.items(), key=lambda x: -len(x[1])):
        examples = ", ".join(e["title"].replace("_", " ") for e in entries[:3])
        if len(entries) > 3:
            examples += f", … (+{len(entries) - 3})"
        w(f"| {pattern} | {len(entries)} | {examples} |")
    w("")

    # Detailed CleanCoNLL errors
    if clean_wrong_aida_ours_agree:
        w("### Details")
        w("")
        w("| Entity | AIDA (correct) | CleanCoNLL (wrong) | Ours | P31 |")
        w("|--------|:--------------:|:------------------:|:----:|-----|")
        for e in clean_wrong_aida_ours_agree:
            ent = resolved.get(e["title"], {})
            p31 = p31_display(ent)
            w(f"| {e['title'].replace('_', ' ')} | {e['aida_ner']} | {e['clean_ner']} | {cat_short(e['category'])} | {p31[:60]} |")
        w("")

    # --- Both NER wrong ---
    w("## Entities Where Both NER Systems Differ From Ours")
    w("")
    w(f"**{len(both_ner_wrong_ours_right)} entities** where both AIDA and CleanCoNLL agree on a NER tag that differs from our ontological classification. These include entity linking errors (disambiguation pages, wrong targets) and genuine NER-vs-ontology boundary cases where both perspectives are defensible.")
    w("")

    both_wrong_patterns: dict[str, list] = {}
    for e in both_ner_wrong_ours_right:
        ent = resolved.get(e["title"], {})
        labels = ent.get("p31_labels", [])
        # Group by first P31 label for readability
        group = labels[0] if labels else "unknown"
        both_wrong_patterns.setdefault(group, []).append(e)

    w("| P31 Type | Count | NER | Ours | Examples |")
    w("|----------|:-----:|:---:|:----:|---------|")
    for p31_type, entries in sorted(both_wrong_patterns.items(), key=lambda x: -len(x[1])):
        ner = entries[0]["aida_ner"]
        cat = cat_short(entries[0]["category"])
        examples = ", ".join(e["title"].replace("_", " ") for e in entries[:3])
        if len(entries) > 3:
            examples += f", … (+{len(entries) - 3})"
        w(f"| {p31_type} | {len(entries)} | {ner} | {cat} | {examples} |")
    w("")

    # --- All disagree ---
    if all_disagree:
        w("## All Three Systems Disagree")
        w("")
        w("| Entity | AIDA | CleanCoNLL | Ours | P31 |")
        w("|--------|:----:|:----------:|:----:|-----|")
        for e in all_disagree:
            ent = resolved.get(e["title"], {})
            p31 = p31_display(ent)
            w(f"| {e['title'].replace('_', ' ')} | {e['aida_ner']} | {e['clean_ner']} | {cat_short(e['category'])} | {p31[:60]} |")
        w("")

    # --- Summary ---
    w("## Summary")
    w("")
    w("| Metric | Value |")
    w("|--------|------:|")
    w(f"| Shared entities | {len(shared):,} |")
    w(f"| Classified | {len(classified):,} ({100*len(classified)/len(shared):.1f}%) |")
    w(f"| All three agree | {len(all_agree):,} |")
    w(f"| AIDA errors detected | {len(aida_wrong_clean_ours_agree)} |")
    w(f"| CleanCoNLL errors detected | {len(clean_wrong_aida_ours_agree)} |")
    w(f"| Both NER differ from ours (boundary + linking) | {len(both_ner_wrong_ours_right)} |")
    w(f"| All three disagree | {len(all_disagree)} |")
    w("")

    return "\n".join(lines)


def main(write_report: bool = False) -> None:
    print("Loading YAML gate definitions …")
    qid_to_cat = load_gate_qids()
    print(f"  {len(qid_to_cat):,} gate QIDs across 8 categories")

    print("\nLoading resolved entities …")
    aida_resolved = load_resolved(AIDA_DIR / "aida_entities.tsv")
    clean_resolved = load_resolved(CLEAN_DIR / "cleanconll_entities.tsv")
    # Merge resolved — same titles should have same QIDs
    resolved = {**clean_resolved, **aida_resolved}
    print(f"  AIDA: {len(aida_resolved):,}, CleanCoNLL: {len(clean_resolved):,}")

    print("\nExtracting entities …")
    aida_ents = extract_aida_entities()
    clean_ents = extract_clean_entities()
    print(f"  AIDA: {len(aida_ents):,}, CleanCoNLL: {len(clean_ents):,}")

    # Build QID → title maps for each dataset
    aida_qid_to_title: dict[str, str] = {}
    for title in aida_ents:
        ent = aida_resolved.get(title)
        if ent:
            aida_qid_to_title.setdefault(ent["qid"], title)

    clean_qid_to_title: dict[str, str] = {}
    for title in clean_ents:
        ent = clean_resolved.get(title)
        if ent:
            clean_qid_to_title.setdefault(ent["qid"], title)

    # Find shared QIDs (handles renamed/canonicalized titles like Côte_d'Ivoire vs Ivory_Coast)
    shared_qids = sorted(set(aida_qid_to_title.keys()) & set(clean_qid_to_title.keys()))
    print(f"  Shared (by QID): {len(shared_qids):,}")

    # Build shared entity records
    shared = []
    for qid in shared_qids:
        aida_title = aida_qid_to_title[qid]
        clean_title = clean_qid_to_title[qid]
        ent = resolved.get(aida_title) or resolved.get(clean_title)
        category = None
        if ent:
            category = classify_entity(ent["p31"], ent["p279"], qid_to_cat)
        shared.append({
            "title": aida_title,  # use AIDA title as canonical
            "clean_title": clean_title,
            "qid": qid,
            "aida_ner": aida_ents[aida_title],
            "clean_ner": clean_ents[clean_title],
            "category": category,
        })

    classified = [e for e in shared if e["category"] is not None]
    unclassified = [e for e in shared if e["category"] is None]

    # Three-way agreement
    all_agree = aida_err = clean_err = both_wrong = all_disagree = 0
    for e in classified:
        aida_ok = is_aligned(e["aida_ner"], e["category"])
        clean_ok = is_aligned(e["clean_ner"], e["category"])
        if aida_ok and clean_ok:
            all_agree += 1
        elif not aida_ok and clean_ok:
            aida_err += 1
        elif aida_ok and not clean_ok:
            clean_err += 1
        elif e["aida_ner"] == e["clean_ner"]:
            both_wrong += 1
        else:
            all_disagree += 1

    print(f"\n{'='*70}")
    print("THREE-WAY AGREEMENT (shared subset)")
    print(f"{'='*70}")
    print(f"  Shared: {len(shared):,}")
    print(f"  Classified: {len(classified):,} ({100*len(classified)/len(shared):.1f}%)")
    print(f"  Unclassified: {len(unclassified):,}")
    print(f"  All three agree: {all_agree:,}")
    print(f"  AIDA wrong, Clean+Ours agree: {aida_err}")
    print(f"  Clean wrong, AIDA+Ours agree: {clean_err}")
    print(f"  Both NER differ from ours: {both_wrong}")
    print(f"  All three disagree: {all_disagree}")

    if write_report:
        report = generate_report(shared, len(aida_qid_to_title), len(clean_qid_to_title), resolved)
        report_path = SCRIPT_DIR / "classification_comparison_report.md"
        report_path.write_text(report)
        print(f"\n  Report written to {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="Generate classification_comparison_report.md")
    args = parser.parse_args()
    main(write_report=args.report)
