#!/usr/bin/env python3
"""
Filter Wikidata dump files to keep only core-tier entities.

Uses src/bulk_import.yaml detection rules (standalone, no TagLoader).
Processes files sequentially, reading zstd-compressed JSONL and writing
filtered output as zstd-compressed JSONL.

Input:  data_dump/wikidata-20260119-all_0001.jsonl.zst
Output: data_dump_core/wikidata-20260119-all_core_0001.jsonl.zst

Usage:
    uv run --project src/python python src/python/filter_core.py
    uv run --project src/python python src/python/filter_core.py \
        --input-dir data_dump --output-dir data_dump_core
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import lmdb
import yaml
import zstandard as zstd


# ============================================================
# Config types (same logic as detect_bulk.py)
# ============================================================

@dataclass
class BulkLookup:
    """Precomputed lookup tables built from bulk_import.yaml."""
    signature_props: dict[str, set[str]]
    signature_value_props: dict[str, dict[str, set[str]]]
    value_extraction_pids: set[str]
    source_all_props: dict[str, set[str]]
    all_bulk_props: set[str]
    strong_props: set[str]
    medium_props: set[str]
    weak_props: set[str]
    people_strong_props: set[str]
    people_weak_props: set[str]
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
                all_bulk_props.add(pid)
                all_for_source.add(pid)
            source_all_props[name] = all_for_source

        curation = config["human_curation_indicators"]
        rules = config["detection_rules"]

        return BulkLookup(
            signature_props=signature_props,
            signature_value_props=signature_value_props,
            value_extraction_pids=value_extraction_pids,
            source_all_props=source_all_props,
            all_bulk_props=all_bulk_props,
            strong_props=set(curation["strong"]),
            medium_props=set(curation["medium"]),
            weak_props=set(curation["weak"]),
            people_strong_props=set(curation["people_strong"]),
            people_weak_props=set(curation["people_weak"]),
            bulk_ratio_threshold=rules["bulk_ratio_threshold"],
            curation_score_protect=rules["curation_score_protect"],
            structural_p31_qids=set(rules.get("structural_p31_qids", [])),
        )


# ============================================================
# Tier classification
# ============================================================

def extract_qid_value(claim: dict) -> str | None:
    """Extract QID from a claim's mainsnak."""
    mainsnak = claim.get("mainsnak", {})
    if mainsnak.get("snaktype") != "value":
        return None
    datavalue = mainsnak.get("datavalue", {})
    value = datavalue.get("value", {})
    return value.get("id")


def classify_entity(
    entity: dict,
    lmdb_txn,
    lmdb_db,
    lookup: BulkLookup,
) -> str | None:
    """
    Classify an entity into a tier: 'core', 'extended', 'structural', or None.
    Returns None for non-items or entities without English labels.
    """
    if entity.get("type") != "item":
        return None

    qid = entity.get("id", "")
    if not qid:
        return None

    # English label from LMDB
    label_bytes = lmdb_txn.get(qid.encode(), db=lmdb_db)
    if label_bytes is None:
        return None

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

    # Rule 1: P31-based structural detection
    if any(v in lookup.structural_p31_qids for v in p31_values):
        return "structural"

    # Compute bulk ratio (exclude P31)
    props_for_ratio = [p for p in property_keys if p != "P31"]
    total_props = len(props_for_ratio)

    if total_props == 0:
        return "core"

    bulk_prop_count = sum(1 for p in props_for_ratio if p in lookup.all_bulk_props)
    bulk_ratio = bulk_prop_count / total_props

    # Signature-based source database matching
    has_signature_match = False
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
            has_signature_match = True
            break
        elif sig_count == 1:
            all_props = lookup.source_all_props.get(src_name, set())
            non_sig_count = sum(
                1 for p in property_keys
                if p != "P31" and p in all_props and p not in sig_set
            )
            if non_sig_count >= 3:
                has_signature_match = True
                break

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

    # Rule 2: Signature-based detection
    if has_signature_match and curation_score <= 1:
        return "extended"

    # Rule 3: Ratio-based detection
    if bulk_ratio >= lookup.bulk_ratio_threshold and curation_score < lookup.curation_score_protect:
        return "extended"

    return "core"


# ============================================================
# Per-file stats
# ============================================================

@dataclass
class FileStats:
    filename: str
    total_lines: int = 0
    items_with_label: int = 0
    core: int = 0
    extended: int = 0
    structural: int = 0


# ============================================================
# File processing
# ============================================================

def make_output_path(input_path: Path, output_dir: Path) -> Path:
    """wikidata-20260119-all_0001.jsonl.zst -> wikidata-20260119-all_core_0001.jsonl.zst"""
    new_name = input_path.name.replace("all_", "all_core_")
    return output_dir / new_name


def process_file(
    input_path: Path,
    output_path: Path,
    lmdb_env,
    lmdb_db,
    lookup: BulkLookup,
) -> FileStats:
    stats = FileStats(filename=input_path.name)

    txn = lmdb_env.begin(db=lmdb_db)

    dctx = zstd.ZstdDecompressor()
    cctx = zstd.ZstdCompressor(level=3)

    with open(input_path, "rb") as fin:
        with open(output_path, "wb") as fout:
            with dctx.stream_reader(fin) as reader:
                text_reader = io.TextIOWrapper(reader, encoding="utf-8")
                with cctx.stream_writer(fout) as writer:
                    for line in text_reader:
                        stats.total_lines += 1
                        line = line.rstrip("\n")
                        if not line:
                            continue

                        try:
                            entity = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        tier = classify_entity(entity, txn, lmdb_db, lookup)
                        if tier is None:
                            continue

                        stats.items_with_label += 1

                        if tier == "core":
                            stats.core += 1
                            writer.write((line + "\n").encode("utf-8"))
                        elif tier == "extended":
                            stats.extended += 1
                        elif tier == "structural":
                            stats.structural += 1

                        if stats.total_lines % 100_000 == 0:
                            print(
                                f"  {input_path.name}: {stats.total_lines:,} lines, "
                                f"{stats.core:,} core",
                                file=sys.stderr,
                            )

    txn.abort()
    return stats


# ============================================================
# Main
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter Wikidata dump to keep only core-tier entities"
    )
    parser.add_argument(
        "--input-dir", type=Path, default=Path("data_dump"),
        help="Input directory containing .jsonl.zst dump files",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data_dump_core"),
        help="Output directory for filtered core files",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("src/bulk_import.yaml"),
        help="Bulk-import YAML config",
    )
    parser.add_argument(
        "--lmdb", type=Path, default=Path("data_dump_output/labels_en.lmdb"),
        help="LMDB database for label lookup",
    )
    args = parser.parse_args()

    # Load config
    config = yaml.safe_load(args.config.read_text())
    lookup = BulkLookup.from_config(config)

    print(
        f"Loaded bulk_import.yaml: {len(config['source_databases'])} source databases, "
        f"{len(config['detection_rules'].get('structural_p31_qids', []))} structural P31 QIDs",
        file=sys.stderr,
    )

    # Open LMDB
    env = lmdb.open(str(args.lmdb), readonly=True, max_dbs=1, map_size=20 * 1024 * 1024 * 1024)
    lmdb_db = env.open_db(b"labels_en")

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Find dump files
    files = sorted(args.input_dir.glob("*.jsonl.zst"))
    if not files:
        print(f"No .jsonl.zst files found in {args.input_dir}", file=sys.stderr)
        return

    print(f"Found {len(files)} dump files", file=sys.stderr)

    all_stats: list[FileStats] = []
    t0 = time.time()

    for f in files:
        output_path = make_output_path(f, args.output_dir)
        print(f"\nProcessing: {f.name}", file=sys.stderr)
        file_t0 = time.time()

        stats = process_file(f, output_path, env, lmdb_db, lookup)
        all_stats.append(stats)

        pct = stats.core / stats.items_with_label * 100 if stats.items_with_label > 0 else 0
        elapsed = time.time() - file_t0
        print(
            f"  Done: {stats.core:,}/{stats.items_with_label:,} core ({pct:.1f}%) in {elapsed:.1f}s",
            file=sys.stderr,
        )

    # Summary table
    total_elapsed = time.time() - t0
    print(f"\n{'File':<50} {'Labeled':>10} {'Core':>10} {'Extended':>10} {'Struct':>10} {'Core%':>7}", file=sys.stderr)
    print("-" * 100, file=sys.stderr)

    total_labeled = total_core = total_ext = total_struct = 0
    for s in all_stats:
        pct = s.core / s.items_with_label * 100 if s.items_with_label > 0 else 0
        print(
            f"{s.filename:<50} {s.items_with_label:>10,} {s.core:>10,} "
            f"{s.extended:>10,} {s.structural:>10,} {pct:>6.1f}%",
            file=sys.stderr,
        )
        total_labeled += s.items_with_label
        total_core += s.core
        total_ext += s.extended
        total_struct += s.structural

    print("-" * 100, file=sys.stderr)
    total_pct = total_core / total_labeled * 100 if total_labeled > 0 else 0
    print(
        f"{'TOTAL':<50} {total_labeled:>10,} {total_core:>10,} "
        f"{total_ext:>10,} {total_struct:>10,} {total_pct:>6.1f}%",
        file=sys.stderr,
    )
    print(f"\nTotal time: {total_elapsed:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
