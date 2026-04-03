#!/usr/bin/env python3
"""
Export classified parquet data to CSV files for neo4j-admin database import.

Uses DuckDB for all heavy lifting: parquet scanning, JSON extraction, CSV writing.
  Pass 1: Collect all QIDs into a temp table (for stub detection)
  Pass 2: COPY per-category node CSVs + per-module edge CSVs via SQL
  Pass 3: Anti-join edge targets vs QIDs → stub nodes

Usage:
  python export_neo4j.py
  python export_neo4j.py --input data_dump_core_output/classified_rust \
      --output data_dump_core_output/neo4j
  python export_neo4j.py --limit 3   # first 3 shards only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import duckdb
import lmdb
import yaml

# ── Category → neo4j label mapping ──────────────────────────────────────────

CATEGORY_LABELS: dict[str, str] = {
    "people": "PERSON",
    "places": "PLACE",
    "science": "SCIENCE",
    "creative_works_media": "WORK",
    "organizations": "ORGANIZATION",
    "events_actions": "EVENT",
    "knowledge_information_abstracts": "KNOWLEDGE",
    "products_artifacts": "PRODUCT",
}

# ── Helpers ──────────────────────────────────────────────────────────────────


def snake_case(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def build_pid_labels(lmdb_path: str, pids: set[str]) -> dict[str, str]:
    """Look up PID → English label from LMDB."""
    env = lmdb.open(lmdb_path, readonly=True, lock=False, max_dbs=1)
    dbi = env.open_db(b"labels_en")
    labels = {}
    with env.begin() as txn:
        for pid in sorted(pids):
            val = txn.get(pid.encode("utf-8"), db=dbi)
            labels[pid] = val.decode("utf-8") if val else pid
    env.close()
    return labels


def _format_quals(quals_json: str) -> str:
    """DuckDB UDF: format qualifiers JSON to human-readable string."""
    if not quals_json:
        return ""
    try:
        quals = json.loads(quals_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    parts = []
    for _pid, data in quals.items():
        label = data.get("label", _pid)
        vals = [v.get("label") or str(v.get("value", "")) for v in data.get("values", [])]
        if vals:
            parts.append(f"{label}: {', '.join(vals)}")
    return "; ".join(parts)


# ── Schema builder ───────────────────────────────────────────────────────────


def build_schemas(tags_dir: Path, lmdb_path: str) -> tuple[
    dict[str, list[tuple[str, str]]],  # core_schema: category → [(pid, col_name)]
    set[str],  # relational module names
]:
    """Parse YAML tag files and build core property schemas + relational module set."""
    all_pids: set[str] = set()
    core_pids_by_cat: dict[str, list[str]] = {}
    relational_modules: set[str] = set()

    for yaml_file in sorted(tags_dir.glob("*.yaml")):
        cat_name = yaml_file.stem
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
        if not data:
            continue
        core_props = data.get("core", {}).get("props", [])
        core_pids_by_cat[cat_name] = core_props
        all_pids.update(core_props)
        for _mod_name, mod_data in data.get("modules", {}).items():
            if mod_data.get("type") == "relational":
                relational_modules.add(_mod_name)

    pid_labels = build_pid_labels(lmdb_path, all_pids)
    core_schema: dict[str, list[tuple[str, str]]] = {}
    for cat_name, pids in core_pids_by_cat.items():
        core_schema[cat_name] = [(pid, snake_case(pid_labels.get(pid, pid))) for pid in pids]

    return core_schema, relational_modules


# ── SQL generators ───────────────────────────────────────────────────────────


def _core_prop_sql(pid: str, col_name: str) -> str:
    """Generate a scalar subquery that extracts a core property's values.

    Filters out deprecated statements, extracts label (or value), joins with ';'.
    """
    return f"""(SELECT COALESCE(string_agg(
        COALESCE(
            json_extract_string(s, '$.label'),
            json_extract_string(s, '$.value'),
            ''
        ), ';'), '')
      FROM (SELECT unnest(COALESCE(
          CAST(json_extract(claims::JSON, '$.core.{pid}.statements') AS JSON[]),
          []::JSON[]
      )) AS s)
      WHERE COALESCE(json_extract_string(s, '$.rank'), 'normal') != 'deprecated'
    ) AS "{col_name}" """


def _node_query(cat_name: str, cat_label: str, core_cols: list[tuple[str, str]]) -> str:
    """Generate the COPY query for a category's node CSV."""
    core_selects = ",\n        ".join(_core_prop_sql(pid, col) for pid, col in core_cols)
    if core_selects:
        core_selects = ",\n        " + core_selects

    return f"""SELECT
        qid AS ":ID",
        COALESCE(label, '') AS "label",
        COALESCE(description, '') AS "description",
        CASE WHEN aliases IS NOT NULL AND aliases != ''
            THEN array_to_string(CAST(aliases::JSON AS VARCHAR[]), ';')
            ELSE '' END AS "aliases:string[]",
        COALESCE(json_extract_string(
            CASE WHEN sitelinks IS NOT NULL THEN sitelinks::JSON END,
            '$.enwiki'), '') AS "enwiki"{core_selects},
        array_to_string(
            list_concat(
                ['ENTITY', '{cat_label}'],
                list_transform(
                    list_filter(
                        list_concat(
                            COALESCE(intrinsic_modules, []),
                            COALESCE(relational_modules, [])
                        ),
                        x -> x IS NOT NULL AND x != ''
                    ),
                    x -> upper(x)
                )
            ), ';'
        ) AS ":LABEL"
    FROM entities
    WHERE category = '{cat_name}'"""


def _all_edges_query() -> str:
    """Single query to extract ALL relational edges from all entities."""
    return """SELECT
        e.qid AS start_id,
        json_extract_string(stmt, '$.value') AS end_id,
        upper(mod) AS rel_type,
        json_extract_string(prop_data, '$.label') AS property,
        prop_key AS pid,
        format_quals(CAST(json_extract(stmt, '$.qualifiers') AS VARCHAR)) AS qualifiers
    FROM entities e,
        LATERAL (SELECT unnest(e.relational_modules) AS mod) mods,
        LATERAL (
            SELECT key AS prop_key, value AS prop_data
            FROM json_each(COALESCE(
                json_extract(e.claims::JSON, concat('$.relational.', mod)),
                '{}'::JSON
            ))
        ) props,
        LATERAL (
            SELECT unnest(COALESCE(
                CAST(json_extract(props.prop_data, '$.statements') AS JSON[]),
                []::JSON[]
            )) AS stmt
        ) stmts
    WHERE e.relational_modules IS NOT NULL
        AND len(e.relational_modules) > 0
        AND json_extract_string(stmt, '$.value') ~ '^Q\\d+$'
        AND COALESCE(json_extract_string(stmt, '$.rank'), 'normal') != 'deprecated'"""


# ── Main ─────────────────────────────────────────────────────────────────────


def print_import_command(output_dir: Path, categories: list[str], modules: list[str]):
    """Print the neo4j-admin database import command."""
    parts = ["neo4j-admin database import full"]
    for cat in sorted(categories):
        short = CATEGORY_LABELS.get(cat, cat.upper()).lower()
        parts.append(f"  --nodes=nodes_{short}.csv")
    if (output_dir / "nodes_stub.csv").exists():
        parts.append("  --nodes=nodes_stub.csv")
    for mod in sorted(modules):
        parts.append(f"  --relationships=edges_{mod}.csv")
    parts.append("  --delimiter=','")
    parts.append("  --array-delimiter=';'")
    parts.append("  --skip-bad-relationships=true")
    parts.append("  --overwrite-destination=true")
    parts.append("  neo4j")
    print("\nImport command:")
    print(" \\\n".join(parts))


def main():
    parser = argparse.ArgumentParser(description="Export classified parquets to neo4j CSV")
    proj_root = Path(__file__).resolve().parents[2]
    default_input = proj_root / "data_dump_core_output" / "classified_rust"
    default_output = proj_root / "data_dump_core_output" / "neo4j"
    default_lmdb = proj_root / "data_dump_output" / "labels_en.lmdb"
    default_tags = proj_root / "src" / "tags"

    parser.add_argument("--input", default=str(default_input), help="Classified parquet directory")
    parser.add_argument("--output", default=str(default_output), help="Output CSV directory")
    parser.add_argument("--lmdb", default=str(default_lmdb), help="LMDB labels path")
    parser.add_argument("--tags", default=str(default_tags), help="YAML tags directory")
    parser.add_argument("--limit", type=int, default=0, help="Limit to first N parquet shards (0 = all)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    lmdb_path = args.lmdb
    tags_dir = Path(args.tags)

    parquet_files = sorted(input_dir.glob("*.parquet"))
    if not parquet_files:
        sys.exit(f"No parquet files in {input_dir}")
    if args.limit > 0:
        parquet_files = parquet_files[: args.limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Using {len(parquet_files)} parquet files from {input_dir}")

    # Build schemas from YAML
    core_schema, relational_modules = build_schemas(tags_dir, lmdb_path)
    print(f"Categories: {len(core_schema)}")
    print(f"Relational modules: {len(relational_modules)}")

    # ── DuckDB setup ──
    con = duckdb.connect()
    pq_list = ", ".join(f"'{f}'" for f in parquet_files)
    con.execute(f"CREATE VIEW entities AS SELECT * FROM read_parquet([{pq_list}])")
    con.create_function("format_quals", _format_quals, ["VARCHAR"], "VARCHAR")

    # ── Pass 1: collect all QIDs ──
    print("\n── Pass 1: collecting QIDs ──")
    t0 = time.time()
    con.execute("CREATE TEMP TABLE all_qids AS SELECT DISTINCT qid FROM entities")
    qid_count = con.execute("SELECT count(*) FROM all_qids").fetchone()[0]
    print(f"  {qid_count:,} unique QIDs in {time.time() - t0:.1f}s")

    # ── Pass 2a: write node CSVs ──
    print("\n── Pass 2a: writing node CSVs ──")
    node_counts: dict[str, int] = {}
    for cat_name, cols in core_schema.items():
        t0 = time.time()
        cat_label = CATEGORY_LABELS.get(cat_name, cat_name.upper())
        fname = f"nodes_{cat_label.lower()}.csv"
        fpath = output_dir / fname

        query = _node_query(cat_name, cat_label, cols)
        con.execute(f"COPY ({query}) TO '{fpath}' (HEADER, DELIMITER ',')")

        cnt = con.execute(
            f"SELECT count(*) FROM entities WHERE category = '{cat_name}'"
        ).fetchone()[0]
        node_counts[cat_name] = cnt
        print(f"  {fname}: {cnt:,} nodes ({time.time() - t0:.1f}s)")

    # ── Pass 2b: materialize all edges (single parquet scan) ──
    print("\n── Pass 2b: extracting all edges ──")
    t0 = time.time()
    con.execute(f"CREATE TEMP TABLE all_edges AS {_all_edges_query()}")
    edge_total = con.execute("SELECT count(*) FROM all_edges").fetchone()[0]
    print(f"  {edge_total:,} total edges in {time.time() - t0:.1f}s")

    # Write per-module edge CSVs from temp table
    print("\n── Pass 2c: writing edge CSVs ──")
    edge_counts: dict[str, int] = {}
    for mod in sorted(relational_modules):
        t0 = time.time()
        mod_upper = mod.upper()
        fname = f"edges_{mod}.csv"
        fpath = output_dir / fname
        con.execute(f"""COPY (
            SELECT start_id AS ":START_ID", end_id AS ":END_ID",
                   rel_type AS ":TYPE", property, pid, qualifiers
            FROM all_edges WHERE rel_type = '{mod_upper}'
        ) TO '{fpath}' (HEADER, DELIMITER ',')""")
        cnt = con.execute(
            f"SELECT count(*) FROM all_edges WHERE rel_type = '{mod_upper}'"
        ).fetchone()[0]
        edge_counts[mod] = cnt
        if cnt > 0:
            print(f"  {fname}: {cnt:,} edges ({time.time() - t0:.1f}s)")

    # ── Pass 3: stub nodes (pure SQL, no LMDB) ──
    print("\n── Pass 3: writing stubs ──")
    t0 = time.time()
    stub_path = output_dir / "nodes_stub.csv"
    con.execute(f"""COPY (
        SELECT DISTINCT end_id AS ":ID", '' AS "label", 'ENTITY' AS ":LABEL"
        FROM all_edges
        WHERE end_id NOT IN (SELECT qid FROM all_qids)
        ORDER BY end_id
    ) TO '{stub_path}' (HEADER, DELIMITER ',')""")
    stub_count = con.execute(f"""
        SELECT count(DISTINCT end_id) FROM all_edges
        WHERE end_id NOT IN (SELECT qid FROM all_qids)
    """).fetchone()[0]
    print(f"  {stub_count:,} stubs ({time.time() - t0:.1f}s)")

    # ── Summary ──
    total_nodes = sum(node_counts.values())
    total_edges = sum(edge_counts.values())
    print(f"\n── Summary ──")
    print(f"  Total nodes: {total_nodes:,}")
    print(f"  Total edges: {total_edges:,}")
    print(f"  Total stubs: {stub_count:,}")

    con.close()
    print_import_command(output_dir, list(core_schema), list(relational_modules))


if __name__ == "__main__":
    main()
