#!/usr/bin/env python3
"""
Extract all QIDs and PIDs from tag YAML files, look up their labels in LMDB,
and output a JSON mapping for the viz.

Usage:
  python export_labels.py
  python export_labels.py --output ../../viz/labels.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import lmdb
import yaml


def collect_ids(obj, ids: set[str]):
    """Recursively collect all QID/PID strings from a nested structure."""
    if isinstance(obj, str):
        if re.fullmatch(r'[QP]\d+', obj):
            ids.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if re.fullmatch(r'[QP]\d+', k):
                ids.add(k)
            collect_ids(v, ids)
    elif isinstance(obj, list):
        for item in obj:
            collect_ids(item, ids)


def main():
    parser = argparse.ArgumentParser(description="Export QID/PID labels for viz")
    default_tags = Path(__file__).resolve().parents[2] / "src" / "tags"
    default_lmdb = Path(__file__).resolve().parents[2] / "data_dump_output" / "labels_en.lmdb"
    default_output = Path(__file__).resolve().parents[2] / "viz" / "labels.json"
    parser.add_argument("--tags", default=str(default_tags), help="Tags directory")
    parser.add_argument("--lmdb", default=str(default_lmdb), help="LMDB path")
    parser.add_argument("--output", default=str(default_output), help="Output JSON")
    args = parser.parse_args()

    # Collect all IDs from YAML files
    ids: set[str] = set()
    tags_dir = Path(args.tags)
    for yaml_file in sorted(tags_dir.glob("*.yaml")):
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
        if data:
            collect_ids(data, ids)

    print(f"Found {len(ids)} unique QIDs/PIDs across tag files")

    # Look up in LMDB
    env = lmdb.open(args.lmdb, readonly=True, lock=False, max_dbs=1)
    dbi = env.open_db(b"labels_en")
    labels = {}
    missing = 0
    with env.begin() as txn:
        for qid in sorted(ids):
            val = txn.get(qid.encode("utf-8"), db=dbi)
            if val:
                labels[qid] = val.decode("utf-8")
            else:
                missing += 1

    env.close()
    print(f"Resolved {len(labels)} labels, {missing} not found")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(labels, indent=2, ensure_ascii=False) + "\n")
    print(f"Written to {out_path}")


if __name__ == "__main__":
    main()
