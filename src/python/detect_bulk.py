#!/usr/bin/env python3
"""
Detect bulk-imported Wikidata entities using src/bulk_import.yaml.

Standalone script -- does NOT use TagLoader or src/tags/.
Reads a single .jsonl.zst dump file, classifies entities into core/extended/structural
tiers, and produces a markdown report with statistics and sample entities.

Usage:
    uv run --project src/python python src/python/detect_bulk.py \
        --file data_dump/wikidata-20260119-all_0001.jsonl.zst

    uv run --project src/python python src/python/detect_bulk.py \
        --file data_dump/wikidata-20260119-all_0001.jsonl.zst \
        --output logs/bulk_report_0001.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import lmdb
import yaml
import zstandard as zstd


# ============================================================
# Config types
# ============================================================

@dataclass
class BulkLookup:
    """Precomputed lookup tables built from bulk_import.yaml."""
    signature_props: dict[str, set[str]]
    signature_value_props: dict[str, dict[str, set[str]]]
    value_extraction_pids: set[str]
    source_all_props: dict[str, set[str]]
    all_bulk_props: set[str]
    prop_to_sources: dict[str, list[str]]
    strong_props: set[str]
    medium_props: set[str]
    weak_props: set[str]
    people_strong_props: set[str]
    people_weak_props: set[str]
    core_bio_props: set[str]
    min_core_for_low: int
    bulk_ratio_threshold: float
    curation_score_protect: int
    structural_p31_qids: set[str]

    @staticmethod
    def from_config(config: dict) -> BulkLookup:
        signature_props: dict[str, set[str]] = {}
        signature_value_props: dict[str, dict[str, set[str]]] = {}
        value_extraction_pids: set[str] = set()
        source_all_props: dict[str, set[str]] = {}
        all_bulk_props: set[str] = set()
        prop_to_sources: dict[str, list[str]] = defaultdict(list)

        for name, db in config["source_databases"].items():
            sigs = set(db["signature_props"])
            signature_props[name] = sigs

            val_sigs: dict[str, set[str]] = {}
            for pid, qids in db.get("signature_value_props", {}).items():
                value_extraction_pids.add(pid)
                val_sigs[pid] = set(qids)
            signature_value_props[name] = val_sigs

            all_for_source: set[str] = set()
            for pid in db["properties"]:
                prop_to_sources[pid].append(name)
                all_bulk_props.add(pid)
                all_for_source.add(pid)
            source_all_props[name] = all_for_source

        curation = config["human_curation_indicators"]
        rules = config["detection_rules"]
        people = config["people_quality"]

        return BulkLookup(
            signature_props=signature_props,
            signature_value_props=signature_value_props,
            value_extraction_pids=value_extraction_pids,
            source_all_props=source_all_props,
            all_bulk_props=all_bulk_props,
            prop_to_sources=dict(prop_to_sources),
            strong_props=set(curation["strong"]),
            medium_props=set(curation["medium"]),
            weak_props=set(curation["weak"]),
            people_strong_props=set(curation["people_strong"]),
            people_weak_props=set(curation["people_weak"]),
            core_bio_props=set(people["core_biographical_props"]),
            min_core_for_low=people["min_core_props_for_low"],
            bulk_ratio_threshold=rules["bulk_ratio_threshold"],
            curation_score_protect=rules["curation_score_protect"],
            structural_p31_qids=set(rules.get("structural_p31_qids", [])),
        )


# ============================================================
# Entity result
# ============================================================

@dataclass
class EntityResult:
    qid: str
    label: str
    tier: str
    tier_reason: str
    bulk_ratio: float
    curation_score: int
    total_props: int
    bulk_prop_count: int
    matched_sources: list[str]
    is_person: bool
    people_quality_tier: str | None
    p31_values: list[str]


# ============================================================
# Statistics
# ============================================================

@dataclass
class Stats:
    total_lines: int = 0
    with_label: int = 0
    tier_core: int = 0
    tier_extended: int = 0
    tier_structural: int = 0
    source_hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    bulk_ratio_histogram: list[int] = field(default_factory=lambda: [0] * 10)
    curation_score_histogram: list[int] = field(default_factory=lambda: [0] * 12)
    people_total: int = 0
    people_quality_high: int = 0
    people_quality_medium: int = 0
    people_quality_low: int = 0
    people_quality_stub: int = 0
    near_threshold_samples: list[EntityResult] = field(default_factory=list)
    false_positive_samples: list[EntityResult] = field(default_factory=list)
    core_samples: list[EntityResult] = field(default_factory=list)
    extended_samples: list[EntityResult] = field(default_factory=list)
    structural_samples: list[EntityResult] = field(default_factory=list)
    signature_detections: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rule_structural_p31: int = 0
    rule_signature: int = 0
    rule_ratio: int = 0
    rule_core: int = 0


MAX_SAMPLES = 20


# ============================================================
# Entity processing
# ============================================================

def extract_qid_value(claim: dict) -> str | None:
    """Extract QID from a claim's mainsnak."""
    mainsnak = claim.get("mainsnak", {})
    if mainsnak.get("snaktype") != "value":
        return None
    datavalue = mainsnak.get("datavalue", {})
    value = datavalue.get("value", {})
    return value.get("id")


def process_entity(
    entity: dict,
    lmdb_txn,
    lmdb_db,
    lookup: BulkLookup,
) -> EntityResult | None:
    if entity.get("type") != "item":
        return None

    qid = entity.get("id", "")
    if not qid:
        return None

    # English label from LMDB
    label_bytes = lmdb_txn.get(qid.encode(), db=lmdb_db)
    if label_bytes is None:
        return None
    label = label_bytes.decode()

    claims = entity.get("claims", {})

    property_keys: set[str] = set()
    p31_values: list[str] = []
    property_values: dict[str, list[str]] = defaultdict(list)

    for pid, claim_list in claims.items():
        property_keys.add(pid)
        need_values = pid == "P31" or pid in lookup.value_extraction_pids
        if need_values:
            for claim in claim_list:
                qid_val = extract_qid_value(claim)
                if qid_val:
                    if pid == "P31":
                        p31_values.append(qid_val)
                    property_values[pid].append(qid_val)

    is_person = "Q5" in p31_values
    is_structural = any(v in lookup.structural_p31_qids for v in p31_values)

    # Compute bulk ratio (exclude P31)
    props_for_ratio = {p for p in property_keys if p != "P31"}
    total_props = len(props_for_ratio)

    if total_props == 0:
        tier = "structural" if is_structural else "core"
        tier_reason = "structural_p31" if is_structural else "core"
        return EntityResult(
            qid=qid, label=label, tier=tier, tier_reason=tier_reason,
            bulk_ratio=0.0, curation_score=0, total_props=0, bulk_prop_count=0,
            matched_sources=[], is_person=is_person,
            people_quality_tier=None, p31_values=p31_values,
        )

    bulk_prop_count = 0
    matched_source_set: set[str] = set()
    for pid in props_for_ratio:
        if pid in lookup.all_bulk_props:
            bulk_prop_count += 1
            for src in lookup.prop_to_sources.get(pid, []):
                matched_source_set.add(src)

    bulk_ratio = bulk_prop_count / total_props

    # Signature-based source database matching
    signature_matched: list[str] = []
    for src_name, sig_set in lookup.signature_props.items():
        presence_sig_count = sum(
            1 for p in property_keys if p != "P31" and p in sig_set
        )
        value_sig_count = 0
        val_sigs = lookup.signature_value_props.get(src_name, {})
        for pid, required_qids in val_sigs.items():
            actual_vals = property_values.get(pid, [])
            if any(v in required_qids for v in actual_vals):
                value_sig_count += 1

        sig_count = presence_sig_count + value_sig_count

        if sig_count >= 2:
            signature_matched.append(src_name)
        elif sig_count == 1:
            all_props = lookup.source_all_props.get(src_name, set())
            non_sig_count = sum(
                1 for p in property_keys
                if p != "P31" and p in all_props and p not in sig_set
            )
            if non_sig_count >= 3:
                signature_matched.append(src_name)

    # Curation score
    curation_score = 0
    for pid in property_keys:
        if pid in lookup.strong_props:
            curation_score += 3
        if pid in lookup.medium_props:
            curation_score += 2
        if pid in lookup.weak_props:
            curation_score += 1
        if is_person and pid in lookup.people_strong_props:
            curation_score += 2
        if is_person and pid in lookup.people_weak_props:
            curation_score += 1

    # Determine tier
    if is_structural:
        tier, tier_reason = "structural", "structural_p31"
    elif signature_matched and curation_score <= 1:
        tier = "extended"
        tier_reason = f"signature:{signature_matched[0]}"
    elif bulk_ratio >= lookup.bulk_ratio_threshold and curation_score < lookup.curation_score_protect:
        tier, tier_reason = "extended", "ratio"
    else:
        tier, tier_reason = "core", "core"

    # People quality tier
    people_quality_tier = None
    if is_person:
        bio_count = sum(1 for p in property_keys if p in lookup.core_bio_props)
        if curation_score >= 12:
            people_quality_tier = "high"
        elif curation_score >= 5:
            people_quality_tier = "medium"
        elif bio_count >= lookup.min_core_for_low:
            people_quality_tier = "low"
        else:
            people_quality_tier = "stub"

    return EntityResult(
        qid=qid, label=label, tier=tier, tier_reason=tier_reason,
        bulk_ratio=bulk_ratio, curation_score=curation_score,
        total_props=total_props, bulk_prop_count=bulk_prop_count,
        matched_sources=list(matched_source_set), is_person=is_person,
        people_quality_tier=people_quality_tier, p31_values=p31_values,
    )


def collect_stats(stats: Stats, result: EntityResult) -> None:
    """Aggregate a single entity result into stats."""
    stats.with_label += 1

    if result.tier == "core":
        stats.tier_core += 1
    elif result.tier == "extended":
        stats.tier_extended += 1
    elif result.tier == "structural":
        stats.tier_structural += 1

    # Track rule
    if result.tier_reason == "structural_p31":
        stats.rule_structural_p31 += 1
    elif result.tier_reason == "ratio":
        stats.rule_ratio += 1
    elif result.tier_reason == "core":
        stats.rule_core += 1
    elif result.tier_reason.startswith("signature:"):
        stats.rule_signature += 1
        src = result.tier_reason[len("signature:"):]
        stats.signature_detections[src] += 1

    for src in result.matched_sources:
        stats.source_hits[src] += 1

    bucket = min(int(result.bulk_ratio * 10), 9)
    stats.bulk_ratio_histogram[bucket] += 1

    cs_bucket = min(result.curation_score, 11)
    stats.curation_score_histogram[cs_bucket] += 1

    if result.is_person:
        stats.people_total += 1
        qt = result.people_quality_tier
        if qt == "high":
            stats.people_quality_high += 1
        elif qt == "medium":
            stats.people_quality_medium += 1
        elif qt == "low":
            stats.people_quality_low += 1
        elif qt == "stub":
            stats.people_quality_stub += 1

    if 0.55 <= result.bulk_ratio <= 0.85 and len(stats.near_threshold_samples) < MAX_SAMPLES:
        stats.near_threshold_samples.append(result)
    if result.tier in ("extended", "structural") and result.curation_score >= 4:
        if len(stats.false_positive_samples) < MAX_SAMPLES:
            stats.false_positive_samples.append(result)
    if result.tier == "core" and len(stats.core_samples) < MAX_SAMPLES:
        stats.core_samples.append(result)
    elif result.tier == "extended" and len(stats.extended_samples) < MAX_SAMPLES:
        stats.extended_samples.append(result)
    elif result.tier == "structural" and len(stats.structural_samples) < MAX_SAMPLES:
        stats.structural_samples.append(result)


# ============================================================
# Report generation
# ============================================================

def entity_table(entities: list[EntityResult]) -> str:
    lines = [
        "| QID | Label | Tier | Reason | Bulk Ratio | Curation | Props | Bulk Props | Sources | Person | Quality |",
        "|-----|-------|------|--------|-----------|----------|-------|------------|---------|--------|---------|",
    ]
    for e in entities:
        label_short = e.label[:27] + "..." if len(e.label) > 30 else e.label
        sources_short = (
            ",".join(e.matched_sources[:3]) + f"+{len(e.matched_sources) - 3}"
            if len(e.matched_sources) > 3
            else ",".join(e.matched_sources)
        )
        quality = e.people_quality_tier or "-"
        person = "Y" if e.is_person else "N"
        lines.append(
            f"| {e.qid} | {label_short} | {e.tier} | {e.tier_reason} | "
            f"{e.bulk_ratio:.2f} | {e.curation_score} | {e.total_props} | "
            f"{e.bulk_prop_count} | {sources_short} | {person} | {quality} |"
        )
    lines.append("")
    return "\n".join(lines)


def generate_report(stats: Stats, file_path: Path) -> str:
    lines: list[str] = []
    a = lines.append

    total = stats.tier_core + stats.tier_extended + stats.tier_structural

    a("# Bulk Import Detection Report")
    a("")
    a(f"File: {file_path}")
    a("")

    a("## Overview")
    a("")
    a("| Metric | Count |")
    a("|--------|-------|")
    a(f"| Total lines | {stats.total_lines} |")
    a(f"| Entities with English label | {stats.with_label} |")
    a("")

    a("## Tier Distribution")
    a("")
    a("| Tier | Count | % |")
    a("|------|-------|---|")
    if total > 0:
        a(f"| core | {stats.tier_core} | {stats.tier_core / total * 100:.1f}% |")
        a(f"| extended | {stats.tier_extended} | {stats.tier_extended / total * 100:.1f}% |")
        a(f"| structural | {stats.tier_structural} | {stats.tier_structural / total * 100:.1f}% |")
        a(f"| **total** | **{total}** | |")
    a("")

    a("## Source Database Hits")
    a("")
    a("| Source Database | Entities | % of labeled |")
    a("|---------------|----------|-------------|")
    for name, count in sorted(stats.source_hits.items(), key=lambda x: -x[1]):
        if stats.with_label > 0:
            a(f"| {name} | {count} | {count / stats.with_label * 100:.1f}% |")
    a("")

    a("## Bulk Ratio Distribution")
    a("")
    a("| Range | Count | % |")
    a("|-------|-------|---|")
    range_labels = [
        "0.0-0.1", "0.1-0.2", "0.2-0.3", "0.3-0.4", "0.4-0.5",
        "0.5-0.6", "0.6-0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0",
    ]
    for i, rl in enumerate(range_labels):
        count = stats.bulk_ratio_histogram[i]
        if total > 0:
            a(f"| {rl} | {count} | {count / total * 100:.1f}% |")
    a("")

    a("## Curation Score Distribution")
    a("")
    a("| Score | Count | % |")
    a("|-------|-------|---|")
    for i in range(12):
        count = stats.curation_score_histogram[i]
        score_label = str(i) if i < 11 else "11+"
        if total > 0:
            a(f"| {score_label} | {count} | {count / total * 100:.1f}% |")
    a("")

    a("## Detection Rule Breakdown")
    a("")
    a("| Rule | Count | % |")
    a("|------|-------|---|")
    if total > 0:
        a(f"| structural_p31 | {stats.rule_structural_p31} | {stats.rule_structural_p31 / total * 100:.1f}% |")
        a(f"| signature | {stats.rule_signature} | {stats.rule_signature / total * 100:.1f}% |")
        a(f"| ratio | {stats.rule_ratio} | {stats.rule_ratio / total * 100:.1f}% |")
        a(f"| core (default) | {stats.rule_core} | {stats.rule_core / total * 100:.1f}% |")
    a("")

    if stats.signature_detections:
        a("### Signature Detections by Source")
        a("")
        a("| Source | Count |")
        a("|--------|-------|")
        for name, count in sorted(stats.signature_detections.items(), key=lambda x: -x[1]):
            a(f"| {name} | {count} |")
        a("")

    if stats.people_total > 0:
        a("## People Quality Tiers")
        a("")
        a("| Quality | Count | % of people |")
        a("|---------|-------|-------------|")
        pt = stats.people_total
        a(f"| high | {stats.people_quality_high} | {stats.people_quality_high / pt * 100:.1f}% |")
        a(f"| medium | {stats.people_quality_medium} | {stats.people_quality_medium / pt * 100:.1f}% |")
        a(f"| low | {stats.people_quality_low} | {stats.people_quality_low / pt * 100:.1f}% |")
        a(f"| stub | {stats.people_quality_stub} | {stats.people_quality_stub / pt * 100:.1f}% |")
        a(f"| **total** | **{pt}** | |")
        a("")

    sample_sections = [
        ("Sample: Near-Threshold Entities (bulk_ratio 0.55-0.85)", stats.near_threshold_samples),
        ("Sample: Potential False Positives (flagged bulk + high curation)", stats.false_positive_samples),
        ("Sample: Core Tier Entities", stats.core_samples),
        ("Sample: Extended Tier Entities", stats.extended_samples),
        ("Sample: Structural Tier Entities", stats.structural_samples),
    ]
    for title, samples in sample_sections:
        if samples:
            a(f"## {title}")
            a("")
            a(entity_table(samples))

    return "\n".join(lines)


# ============================================================
# Main
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Detect bulk-imported Wikidata entities")
    parser.add_argument("--file", type=Path, required=True, help="Single dump file (.jsonl.zst)")
    parser.add_argument("--config", type=Path, default=Path("src/bulk_import.yaml"), help="Bulk-import YAML config")
    parser.add_argument("--lmdb", type=Path, default=Path("data_dump_output/labels_en.lmdb"), help="LMDB database path")
    parser.add_argument("--output", type=Path, help="Output report file (default: stdout)")
    args = parser.parse_args()

    # Load config
    config = yaml.safe_load(args.config.read_text())
    lookup = BulkLookup.from_config(config)

    n_curation = (
        len(config["human_curation_indicators"]["strong"])
        + len(config["human_curation_indicators"]["medium"])
        + len(config["human_curation_indicators"]["weak"])
        + len(config["human_curation_indicators"]["people_strong"])
        + len(config["human_curation_indicators"]["people_weak"])
    )
    print(
        f"Loaded bulk_import.yaml: {len(config['source_databases'])} source databases, "
        f"{n_curation} curation indicators",
        file=sys.stderr,
    )

    # Open LMDB
    env = lmdb.open(str(args.lmdb), readonly=True, max_dbs=1, map_size=20 * 1024 * 1024 * 1024)
    lmdb_db = env.open_db(b"labels_en")

    file_size = args.file.stat().st_size
    print(
        f"Processing: {args.file} ({file_size / 1048576:.1f} MB compressed)",
        file=sys.stderr,
    )

    stats = Stats()
    t0 = time.time()

    # Read and process
    dctx = zstd.ZstdDecompressor()
    with open(args.file, "rb") as fh:
        with dctx.stream_reader(fh) as reader:
            import io
            text_reader = io.TextIOWrapper(reader, encoding="utf-8")
            txn = env.begin(db=lmdb_db)
            for line in text_reader:
                stats.total_lines += 1
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    entity = json.loads(line)
                except json.JSONDecodeError:
                    continue

                result = process_entity(entity, txn, lmdb_db, lookup)
                if result:
                    collect_stats(stats, result)

                if stats.total_lines % 100_000 == 0:
                    print(
                        f"  {stats.total_lines:,} lines, {stats.with_label:,} labeled, "
                        f"{stats.tier_core:,} core",
                        file=sys.stderr,
                    )
            txn.abort()

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s ({stats.total_lines:,} lines)", file=sys.stderr)

    report = generate_report(stats, args.file)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
