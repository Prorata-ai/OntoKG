#!/usr/bin/env python3
"""Find high-reference unclassified hub entities in classified parquet files.

These are entities frequently referenced as relational edge targets but which
have no category assignment — gaps in YAML gate QID coverage. Examples: religions,
sports, artistic movements, etc.

Outputs:
  - data_dump_core_output/unclassified_hubs.csv
      All unclassified targets with ref_count >= --min-refs
  - data_dump_core_output/unclassified_hub_types.csv
      P31 type summary grouped by instance-of QID

Usage:
  uv run --project src/python python src/python/find_unclassified_hubs.py
  uv run --project src/python python src/python/find_unclassified_hubs.py --min-refs 50
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb
import lmdb


def resolve_labels(qids: list[str], lmdb_path: str) -> dict[str, str]:
    """Batch-resolve QIDs to English labels via LMDB."""
    labels = {}
    env = lmdb.open(lmdb_path, readonly=True, lock=False, max_dbs=1)
    dbi = env.open_db(b"labels_en")
    with env.begin() as txn:
        for qid in qids:
            val = txn.get(qid.encode("utf-8"), db=dbi)
            labels[qid] = val.decode("utf-8") if val else qid
    env.close()
    return labels


def main():
    parser = argparse.ArgumentParser(
        description="Find high-reference unclassified hub entities"
    )
    default_input = (
        Path(__file__).resolve().parents[2]
        / "data_dump_core_output"
        / "classified_rust"
    )
    default_output = Path(__file__).resolve().parents[2] / "data_dump_core_output"
    default_lmdb = (
        Path(__file__).resolve().parents[2] / "data_dump_output" / "labels_en.lmdb"
    )

    parser.add_argument("--input", type=Path, default=default_input)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--lmdb-path", type=Path, default=default_lmdb)
    parser.add_argument("--min-refs", type=int, default=10)
    args = parser.parse_args()

    parquet_glob = str(args.input / "*.parquet")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("SET threads = 8")

    # -------------------------------------------------------------------------
    # Step 1: Extract all relational edge target QIDs and count references
    # -------------------------------------------------------------------------
    print("Step 1: Extracting relational edge targets from all parquets...")
    t0 = time.time()

    # Claims structure: relational -> module -> PID -> {label, statements: [{value, ...}]}
    # We need to extract the "value" field from each statement object.
    con.execute("""
        CREATE TABLE ref_counts AS
        WITH
        -- Read all parquets and extract the relational sub-object
        base AS (
            SELECT
                qid AS source_qid,
                claims::JSON AS claims_json
            FROM read_parquet(?)
            WHERE claims IS NOT NULL
        ),
        -- Get module names from relational object
        rel_modules AS (
            SELECT
                source_qid,
                claims_json,
                unnest(json_keys(json_extract(claims_json, '$.relational'))) AS module_name
            FROM base
            WHERE json_extract(claims_json, '$.relational') IS NOT NULL
              AND json_type(json_extract(claims_json, '$.relational')) = 'OBJECT'
              AND len(json_keys(json_extract(claims_json, '$.relational'))) > 0
        ),
        -- Get property IDs within each module
        rel_props AS (
            SELECT
                source_qid,
                claims_json,
                module_name,
                unnest(json_keys(json_extract(claims_json, '$.relational.' || module_name))) AS pid
            FROM rel_modules
        ),
        -- Get individual statements for each property
        -- Each PID maps to {label: ..., statements: [{value: "Q...", ...}, ...]}
        rel_stmts AS (
            SELECT
                source_qid,
                json_extract_string(
                    unnest(
                        CAST(json_extract(claims_json,
                            '$.relational.' || module_name || '.' || pid || '.statements'
                        ) AS JSON[])
                    ),
                    '$.value'
                ) AS target_qid
            FROM rel_props
        )
        SELECT
            target_qid,
            COUNT(DISTINCT source_qid) AS ref_count
        FROM rel_stmts
        WHERE target_qid IS NOT NULL
          AND target_qid LIKE 'Q%'
        GROUP BY target_qid
    """, [parquet_glob])

    count_row = con.execute("SELECT COUNT(*) FROM ref_counts").fetchone()
    print(f"  Found {count_row[0]:,} distinct target QIDs in {time.time() - t0:.1f}s")

    # -------------------------------------------------------------------------
    # Step 2: Join targets back to parquets to get category, label, p31_values
    # -------------------------------------------------------------------------
    print("Step 2: Joining targets to parquet data for classification info...")
    t1 = time.time()

    con.execute("""
        CREATE TABLE target_info AS
        SELECT
            r.target_qid AS qid,
            r.ref_count,
            e.category,
            e.label,
            e.p31_values
        FROM ref_counts r
        LEFT JOIN (
            SELECT DISTINCT ON (qid)
                qid, category, label, p31_values
            FROM read_parquet(?)
        ) e ON r.target_qid = e.qid
    """, [parquet_glob])

    print(f"  Join complete in {time.time() - t1:.1f}s")

    # Stats
    stats = con.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE category IS NOT NULL) AS classified,
            COUNT(*) FILTER (WHERE category IS NULL) AS unclassified,
            SUM(ref_count) FILTER (WHERE category IS NULL) AS unclassified_refs
        FROM target_info
        WHERE ref_count >= ?
    """, [args.min_refs]).fetchone()
    print(f"  Targets with >= {args.min_refs} refs: {stats[0]:,} total, "
          f"{stats[1]:,} classified, {stats[2]:,} unclassified "
          f"({stats[3]:,} total refs to unclassified)")

    # -------------------------------------------------------------------------
    # Step 3: Export unclassified hubs CSV
    # -------------------------------------------------------------------------
    print("Step 3: Exporting unclassified hubs...")
    t2 = time.time()

    # Get all unclassified targets with sufficient refs
    unclassified = con.execute("""
        SELECT
            qid,
            label,
            ref_count,
            p31_values
        FROM target_info
        WHERE category IS NULL
          AND ref_count >= ?
        ORDER BY ref_count DESC
    """, [args.min_refs]).fetchdf()

    # Collect all P31 QIDs for label resolution
    def _is_valid_list(val):
        """Check if a value is a non-null list (handles pandas NA, numpy arrays, etc.)."""
        return isinstance(val, (list, tuple)) or (
            hasattr(val, "__iter__") and not isinstance(val, (str, float, int))
        )

    all_p31_qids = set()
    all_entity_qids = set(unclassified["qid"].tolist())
    for p31_vals in unclassified["p31_values"]:
        if _is_valid_list(p31_vals):
            for v in p31_vals:
                if v:
                    all_p31_qids.add(v)

    print(f"  Resolving labels for {len(all_p31_qids)} P31 QIDs "
          f"and {len(all_entity_qids)} entity QIDs via LMDB...")
    all_qids_to_resolve = list(all_p31_qids | all_entity_qids)
    labels = resolve_labels(all_qids_to_resolve, str(args.lmdb_path))

    # Build p31_labels column
    p31_labels_col = []
    for p31_vals in unclassified["p31_values"]:
        if _is_valid_list(p31_vals):
            p31_labels_col.append(
                "; ".join(f"{v} ({labels.get(v, v)})" for v in p31_vals if v)
            )
        else:
            p31_labels_col.append("")

    unclassified["p31_labels"] = p31_labels_col

    # Fill missing labels from LMDB
    unclassified["label"] = unclassified.apply(
        lambda row: row["label"]
        if row["label"] is not None and str(row["label"]) not in ("", "None", "nan")
        else labels.get(row["qid"], ""),
        axis=1,
    )

    # Format p31_values as semicolon-separated string
    unclassified["p31_values"] = unclassified["p31_values"].apply(
        lambda v: "; ".join(v) if _is_valid_list(v) else ""
    )

    hubs_path = output_dir / "unclassified_hubs.csv"
    unclassified[["qid", "label", "ref_count", "p31_values", "p31_labels"]].to_csv(
        hubs_path, index=False
    )
    print(f"  Wrote {len(unclassified):,} rows to {hubs_path} in {time.time() - t2:.1f}s")

    # -------------------------------------------------------------------------
    # Step 4: Group by P31 type and export summary
    # -------------------------------------------------------------------------
    print("Step 4: Grouping unclassified hubs by P31 type...")
    t3 = time.time()

    # Unnest p31_values for unclassified entities
    con.execute("""
        CREATE TABLE unclassified_p31 AS
        SELECT
            qid,
            label,
            ref_count,
            unnest(p31_values) AS p31_qid
        FROM target_info
        WHERE category IS NULL
          AND ref_count >= ?
          AND p31_values IS NOT NULL
          AND len(p31_values) > 0
    """, [args.min_refs])

    type_summary = con.execute("""
        SELECT
            p31_qid,
            COUNT(*) AS entity_count,
            SUM(ref_count) AS total_refs,
            ROUND(SUM(ref_count) * 1.0 / COUNT(*), 1) AS refs_per_entity,
            STRING_AGG(label || ' (' || ref_count || ')', '; ' ORDER BY ref_count DESC)
                FILTER (WHERE row_num <= 3) AS top_examples
        FROM (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY p31_qid ORDER BY ref_count DESC) AS row_num
            FROM unclassified_p31
        )
        GROUP BY p31_qid
        ORDER BY total_refs DESC
    """).fetchdf()

    # Resolve P31 labels
    p31_qid_list = type_summary["p31_qid"].tolist()
    p31_labels = resolve_labels(p31_qid_list, str(args.lmdb_path))
    type_summary["p31_label"] = type_summary["p31_qid"].map(p31_labels)

    types_path = output_dir / "unclassified_hub_types.csv"
    type_summary[
        ["p31_qid", "p31_label", "entity_count", "total_refs", "refs_per_entity", "top_examples"]
    ].to_csv(types_path, index=False)
    print(f"  Wrote {len(type_summary):,} rows to {types_path} in {time.time() - t3:.1f}s")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    total_time = time.time() - t0
    print(f"\nDone in {total_time:.1f}s total.")
    print(f"\nTop 20 unclassified hub types by total references:")
    print(f"{'P31 QID':<12} {'Label':<40} {'Entities':>10} {'Total Refs':>12} {'Refs/Ent':>10}")
    print("-" * 88)
    for _, row in type_summary.head(20).iterrows():
        print(
            f"{row['p31_qid']:<12} {row['p31_label'][:40]:<40} "
            f"{row['entity_count']:>10,} {row['total_refs']:>12,} "
            f"{row['refs_per_entity']:>10.1f}"
        )

    print(f"\nTop 20 unclassified hub entities by reference count:")
    print(f"{'QID':<12} {'Label':<50} {'Refs':>10} {'P31 Types'}")
    print("-" * 100)
    for _, row in unclassified.head(20).iterrows():
        p31_short = row["p31_labels"][:60] + "..." if len(row["p31_labels"]) > 60 else row["p31_labels"]
        print(
            f"{row['qid']:<12} {str(row['label'])[:50]:<50} "
            f"{row['ref_count']:>10,} {p31_short}"
        )

    con.close()


if __name__ == "__main__":
    main()
