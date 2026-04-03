"""Analyze P31 (instance of) distribution for a given category.

Adapts analyze_people_p106.py for P31-based analysis. For non-people categories,
P31 is the key discriminating property (unlike people where P106 matters more).

Uses DuckDB for fast parquet + JSON processing (no Python-level row loops).

Reads entity_{category}.parquet, extracts P31 values, and produces:
1. Coverage statistics
2. Top P31 QID frequencies (with labels from LMDB)
3. P31 × module cross-tabulation
4. P31 values with highest unmodulated rate
5. P31 values with most unmodulated count
6. Property profile for top unmodulated P31 types

Results are saved to data_dump_output/{category}_analysis/ as CSV files.
"""

import argparse
import json
import os

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


def resolve_pids(pids: list[str], lmdb_path: str) -> dict[str, str]:
    """Batch-resolve PIDs (property IDs) to English labels via LMDB."""
    labels = {}
    env = lmdb.open(lmdb_path, readonly=True, lock=False, max_dbs=1)
    dbi = env.open_db(b"labels_en")
    with env.begin() as txn:
        for pid in pids:
            val = txn.get(pid.encode("utf-8"), db=dbi)
            labels[pid] = val.decode("utf-8") if val else pid
    env.close()
    return labels


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze P31 distribution for a category")
    parser.add_argument(
        "--input",
        required=True,
        help="Input parquet file (e.g. entity_science.parquet)",
    )
    parser.add_argument(
        "--category",
        required=True,
        help="Category name (used for output directory and labels)",
    )
    parser.add_argument(
        "--lmdb",
        default="data_dump_output/labels_en.lmdb",
        help="LMDB path for label resolution",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for output CSV files (default: data_dump_output/{category}_analysis)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=50,
        help="Number of top P31 values to show",
    )
    args = parser.parse_args()

    if args.output_dir is None:
        args.output_dir = f"data_dump_output/{args.category}_analysis"

    os.makedirs(args.output_dir, exist_ok=True)

    con = duckdb.connect()

    # Create a view for the parquet file
    con.execute(f"CREATE VIEW entities AS SELECT * FROM read_parquet('{args.input}')")

    n_total = con.execute("SELECT count(*) FROM entities").fetchone()[0]
    print(f"Total {args.category} entities: {n_total:,}\n")

    # ── Explode P31 from JSON properties into a usable table ──
    print("Extracting P31 values (DuckDB JSON + unnest)...")
    con.execute("""
        CREATE TABLE p31_expanded AS
        SELECT
            qid,
            unnest(
                CASE
                    WHEN json_extract_string(properties, '$.P31') IS NOT NULL
                    THEN from_json(json_extract(properties, '$.P31'), '["VARCHAR"]')
                    ELSE CAST([] AS VARCHAR[])
                END
            ) AS p31_qid,
            modules,
            len(modules) AS n_modules,
            properties,
            CASE
                WHEN json_extract_string(properties, '$.P31') IS NOT NULL
                THEN len(from_json(json_extract(properties, '$.P31'), '["VARCHAR"]'))
                ELSE 0
            END AS n_p31
        FROM entities
        WHERE json_extract_string(properties, '$.P31') IS NOT NULL
    """)

    # Also create a summary per entity (for coverage stats)
    print("Computing per-entity summary...")
    con.execute("""
        CREATE TABLE entity_summary AS
        SELECT
            qid,
            CASE
                WHEN json_extract_string(properties, '$.P31') IS NOT NULL
                THEN len(from_json(json_extract(properties, '$.P31'), '["VARCHAR"]'))
                ELSE 0
            END AS n_p31,
            len(modules) AS n_modules
        FROM entities
    """)

    # ══════════════════════════════════════════════════════════════════
    # Section 1: Coverage statistics
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print(f"COVERAGE STATISTICS — {args.category.upper()}")
    print("=" * 70)

    stats = con.execute("""
        SELECT
            count(*) AS total,
            count(*) FILTER (n_p31 > 0) AS with_p31,
            count(*) FILTER (n_modules > 0) AS with_modules,
            count(*) FILTER (n_p31 > 0 AND n_modules > 0) AS with_both,
            count(*) FILTER (n_modules = 0) AS no_modules,
            count(*) FILTER (n_p31 > 0 AND n_modules = 0) AS p31_no_modules
        FROM entity_summary
    """).fetchone()

    total, with_p31, with_modules, with_both, no_modules, p31_no_mod = stats
    print(f"Total entities:            {total:>12,}")
    print(f"With P31 (instance of):    {with_p31:>12,}  ({with_p31/total*100:.1f}%)")
    print(f"With modules:              {with_modules:>12,}  ({with_modules/total*100:.1f}%)")
    print(f"With both:                 {with_both:>12,}  ({with_both/total*100:.1f}%)")
    print(f"No modules:                {no_modules:>12,}  ({no_modules/total*100:.1f}%)")
    print(f"P31 but no modules:        {p31_no_mod:>12,}  ({p31_no_mod/total*100:.1f}%)")

    # P31 count distribution
    print(f"\nP31 count distribution (types per entity):")
    p31_dist = con.execute("""
        SELECT n_p31, count(*) AS cnt
        FROM entity_summary
        GROUP BY n_p31 ORDER BY n_p31
    """).fetchall()
    for n, cnt in p31_dist[:10]:
        print(f"  {n} types: {cnt:>10,}")
    rest = sum(cnt for n, cnt in p31_dist if n >= 10)
    if rest:
        print(f"  10+ types: {rest:>10,}")

    # Module count distribution
    print(f"\nModule count distribution (modules per entity):")
    mod_dist = con.execute("""
        SELECT n_modules, count(*) AS cnt
        FROM entity_summary
        GROUP BY n_modules ORDER BY n_modules
    """).fetchall()
    for n, cnt in mod_dist:
        print(f"  {n} modules: {cnt:>10,}")

    # Module frequencies
    print(f"\nModule frequencies:")
    mod_freq = con.execute("""
        SELECT mod, count(*) AS cnt
        FROM (SELECT unnest(modules) AS mod FROM entities WHERE len(modules) > 0)
        GROUP BY mod ORDER BY cnt DESC
    """).fetchall()
    for mod, cnt in mod_freq:
        print(f"  {mod:<20s} {cnt:>10,}  ({cnt/total*100:.1f}%)")

    # ══════════════════════════════════════════════════════════════════
    # Section 2: Top P31 values
    # ══════════════════════════════════════════════════════════════════
    print("\nComputing P31 frequencies...")
    p31_top = con.execute(f"""
        SELECT p31_qid, count(*) AS cnt
        FROM p31_expanded
        GROUP BY p31_qid
        ORDER BY cnt DESC
        LIMIT {args.top_n}
    """).fetchall()

    unique_p31 = con.execute("SELECT count(DISTINCT p31_qid) FROM p31_expanded").fetchone()[0]

    # Collect all QIDs we need labels for
    all_qids = set(row[0] for row in p31_top)

    # Also get top unmodulated P31
    p31_no_mod_top = con.execute(f"""
        SELECT p31_qid, count(*) AS cnt
        FROM p31_expanded
        WHERE n_modules = 0
        GROUP BY p31_qid
        ORDER BY cnt DESC
        LIMIT {args.top_n}
    """).fetchall()
    all_qids.update(row[0] for row in p31_no_mod_top)

    # Get high-unmodulated-rate QIDs (min 500 entities)
    p31_unmod_rate = con.execute("""
        SELECT
            p31_qid,
            count(*) AS total,
            count(*) FILTER (n_modules = 0) AS no_mod,
            count(*) FILTER (n_modules = 0) * 1.0 / count(*) AS rate
        FROM p31_expanded
        GROUP BY p31_qid
        HAVING count(*) >= 500
        ORDER BY rate DESC
        LIMIT 40
    """).fetchall()
    all_qids.update(row[0] for row in p31_unmod_rate)

    print(f"Resolving {len(all_qids)} QID labels from LMDB...")
    labels = resolve_labels(list(all_qids), args.lmdb)

    # Register labels as a DuckDB table for joins
    label_rows = [(qid, label) for qid, label in labels.items()]
    con.execute("CREATE TABLE labels(qid VARCHAR, label VARCHAR)")
    con.executemany("INSERT INTO labels VALUES (?, ?)", label_rows)

    print("\n" + "=" * 70)
    print(f"TOP {args.top_n} P31 (INSTANCE OF) VALUES — {args.category.upper()}")
    print("=" * 70)
    print(f"{'Rank':<6}{'QID':<12}{'Label':<40}{'Count':>10}  {'%':>6}")
    print("-" * 76)
    for rank, (qid, cnt) in enumerate(p31_top, 1):
        label = labels.get(qid, qid)
        print(f"{rank:<6}{qid:<12}{label:<40}{cnt:>10,}  {cnt/total*100:>5.2f}%")
    print(f"\nUnique P31 QIDs: {unique_p31:,}")

    # Save to CSV
    con.execute(f"""
        COPY (
            SELECT
                p.p31_qid AS qid,
                COALESCE(l.label, p.p31_qid) AS label,
                p.cnt AS count
            FROM (
                SELECT p31_qid, count(*) AS cnt
                FROM p31_expanded
                GROUP BY p31_qid
                ORDER BY cnt DESC
            ) p
            LEFT JOIN labels l ON p.p31_qid = l.qid
        ) TO '{args.output_dir}/p31_frequencies.csv' (HEADER, DELIMITER ',')
    """)
    print(f"  -> Saved to {args.output_dir}/p31_frequencies.csv")

    # ══════════════════════════════════════════════════════════════════
    # Section 3: P31 × Module cross-tabulation
    # ══════════════════════════════════════════════════════════════════
    module_names = [row[0] for row in mod_freq]

    if module_names:
        print("\n" + "=" * 70)
        print(f"TOP 50 P31 x MODULE CROSS-TABULATION — {args.category.upper()}")
        print("=" * 70)

        # Build pivot query
        pivot_cols = ", ".join(
            f"count(*) FILTER (mod = '{m}') AS \"{m}\"" for m in module_names
        )
        cross_tab = con.execute(f"""
            WITH top50 AS (
                SELECT p31_qid, count(*) AS total
                FROM p31_expanded
                GROUP BY p31_qid
                ORDER BY total DESC
                LIMIT 50
            ),
            exploded AS (
                SELECT p.p31_qid, unnest(p.modules) AS mod
                FROM p31_expanded p
                INNER JOIN top50 t ON p.p31_qid = t.p31_qid
                WHERE len(p.modules) > 0
            ),
            no_mod AS (
                SELECT p.p31_qid, count(*) AS none_cnt
                FROM p31_expanded p
                INNER JOIN top50 t ON p.p31_qid = t.p31_qid
                WHERE p.n_modules = 0
                GROUP BY p.p31_qid
            ),
            pivoted AS (
                SELECT p31_qid, {pivot_cols}
                FROM exploded
                GROUP BY p31_qid
            )
            SELECT
                t.p31_qid,
                COALESCE(l.label, t.p31_qid) AS label,
                {', '.join(f'COALESCE(p."{m}", 0) AS "{m}"' for m in module_names)},
                COALESCE(n.none_cnt, 0) AS none,
                t.total
            FROM top50 t
            LEFT JOIN pivoted p ON t.p31_qid = p.p31_qid
            LEFT JOIN no_mod n ON t.p31_qid = n.p31_qid
            LEFT JOIN labels l ON t.p31_qid = l.qid
            ORDER BY t.total DESC
        """).fetchall()

        # Header
        header = f"{'QID':<12}{'Label':<30}"
        for mod in module_names:
            header += f"{mod[:8]:>9}"
        header += f"{'none':>9}{'total':>10}"
        print(header)
        print("-" * len(header))

        for row in cross_tab:
            qid, label = row[0], row[1][:29]
            line = f"{qid:<12}{label:<30}"
            for val in row[2:-2]:  # module columns
                line += f"{val:>9,}" if val > 0 else f"{'·':>9}"
            line += f"{row[-2]:>9,}{row[-1]:>10,}"  # none, total
            print(line)

        # Save cross-tab CSV (top 100)
        con.execute(f"""
            COPY (
                SELECT * FROM (
                    SELECT
                        t.p31_qid AS qid,
                        COALESCE(l.label, t.p31_qid) AS label,
                        {', '.join(f'COALESCE(p."{m}", 0) AS "{m}"' for m in module_names)},
                        COALESCE(n.none_cnt, 0) AS none,
                        t.total
                    FROM (
                        SELECT p31_qid, count(*) AS total
                        FROM p31_expanded GROUP BY p31_qid ORDER BY total DESC LIMIT 100
                    ) t
                    LEFT JOIN (
                        SELECT p31_qid, {pivot_cols}
                        FROM (
                            SELECT p.p31_qid, unnest(p.modules) AS mod
                            FROM p31_expanded p
                            WHERE len(p.modules) > 0
                        ) GROUP BY p31_qid
                    ) p ON t.p31_qid = p.p31_qid
                    LEFT JOIN (
                        SELECT p31_qid, count(*) AS none_cnt
                        FROM p31_expanded WHERE n_modules = 0
                        GROUP BY p31_qid
                    ) n ON t.p31_qid = n.p31_qid
                    LEFT JOIN labels l ON t.p31_qid = l.qid
                    ORDER BY t.total DESC
                )
            ) TO '{args.output_dir}/p31_module_crosstab.csv' (HEADER, DELIMITER ',')
        """)
        print(f"  -> Saved to {args.output_dir}/p31_module_crosstab.csv")
    else:
        print("\nNo modules found — skipping cross-tabulation.")

    # ══════════════════════════════════════════════════════════════════
    # Section 4: P31 values with highest unmodulated rate
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print(f"P31 VALUES WITH HIGHEST UNMODULATED RATE (min 500 entities) — {args.category.upper()}")
    print("=" * 70)
    print(f"{'QID':<12}{'Label':<35}{'Total':>8}{'No-mod':>8}{'Rate':>8}")
    print("-" * 73)
    for qid, total_q, no_mod_q, rate in p31_unmod_rate:
        label = labels.get(qid, qid)
        print(f"{qid:<12}{label:<35}{total_q:>8,}{no_mod_q:>8,}{rate:>7.1%}")

    # Save
    con.execute(f"""
        COPY (
            SELECT
                p31_qid AS qid,
                COALESCE(l.label, p31_qid) AS label,
                count(*) AS total,
                count(*) FILTER (n_modules = 0) AS no_mod,
                round(count(*) FILTER (n_modules = 0) * 100.0 / count(*), 1) AS unmod_rate_pct
            FROM p31_expanded
            LEFT JOIN labels l ON p31_qid = l.qid
            GROUP BY p31_qid, l.label
            HAVING count(*) >= 500
            ORDER BY unmod_rate_pct DESC
        ) TO '{args.output_dir}/p31_unmod_rate.csv' (HEADER, DELIMITER ',')
    """)
    print(f"  -> Saved to {args.output_dir}/p31_unmod_rate.csv")

    # ══════════════════════════════════════════════════════════════════
    # Section 5: P31 values with most unmodulated count
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print(f"TOP {args.top_n} P31 VALUES FOR ENTITIES WITH NO MODULES — {args.category.upper()}")
    print("=" * 70)
    print(f"{'Rank':<6}{'QID':<12}{'Label':<40}{'Count':>10}  {'% of no-mod':>11}")
    print("-" * 81)
    for rank, (qid, cnt) in enumerate(p31_no_mod_top, 1):
        label = labels.get(qid, qid)
        pct = cnt / no_modules * 100 if no_modules > 0 else 0
        print(f"{rank:<6}{qid:<12}{label:<40}{cnt:>10,}  {pct:>10.2f}%")

    # Save
    con.execute(f"""
        COPY (
            SELECT
                p.p31_qid AS qid,
                COALESCE(l.label, p.p31_qid) AS label,
                p.cnt AS count,
                round(p.cnt * 100.0 / {max(no_modules, 1)}, 2) AS pct_of_no_mod
            FROM (
                SELECT p31_qid, count(*) AS cnt
                FROM p31_expanded WHERE n_modules = 0
                GROUP BY p31_qid ORDER BY cnt DESC
            ) p
            LEFT JOIN labels l ON p.p31_qid = l.qid
        ) TO '{args.output_dir}/p31_no_modules.csv' (HEADER, DELIMITER ',')
    """)
    print(f"  -> Saved to {args.output_dir}/p31_no_modules.csv")

    # ══════════════════════════════════════════════════════════════════
    # Section 6: Property profile for top unmodulated P31 types
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print(f"PROPERTY PROFILE FOR TOP 10 UNMODULATED P31 TYPES — {args.category.upper()}")
    print("=" * 70)

    # Get top 10 unmodulated P31 types by count
    top_unmod_p31 = p31_no_mod_top[:10]

    all_prop_profiles = []

    for rank, (p31_qid, unmod_count) in enumerate(top_unmod_p31, 1):
        p31_label = labels.get(p31_qid, p31_qid)
        print(f"\n--- {rank}. {p31_label} ({p31_qid}) — {unmod_count:,} unmodulated entities ---")

        # Get the properties JSON for unmodulated entities of this P31 type
        # We'll extract all property keys and count them
        props_rows = con.execute(f"""
            SELECT properties
            FROM p31_expanded
            WHERE p31_qid = '{p31_qid}' AND n_modules = 0
            LIMIT 50000
        """).fetchall()

        # Count property occurrences across entities
        pid_counts: dict[str, int] = {}
        for (props_json,) in props_rows:
            try:
                props = json.loads(props_json)
                for pid in props:
                    pid_counts[pid] = pid_counts.get(pid, 0) + 1
            except (json.JSONDecodeError, TypeError):
                continue

        if not pid_counts:
            print("  (no properties found)")
            continue

        # Sort by count descending, take top 20
        top_pids = sorted(pid_counts.items(), key=lambda x: -x[1])[:20]
        pid_list = [pid for pid, _ in top_pids]

        # Resolve PID labels
        pid_labels = resolve_pids(pid_list, args.lmdb)

        sample_size = len(props_rows)
        print(f"  Sample size: {sample_size:,} entities (of {unmod_count:,})")
        print(f"  {'PID':<10}{'Label':<40}{'Count':>8}  {'%':>6}")
        print(f"  {'-'*66}")
        for pid, cnt in top_pids:
            plabel = pid_labels.get(pid, pid)
            print(f"  {pid:<10}{plabel:<40}{cnt:>8,}  {cnt/sample_size*100:>5.1f}%")

        # Collect for CSV
        for pid, cnt in top_pids:
            plabel = pid_labels.get(pid, pid)
            all_prop_profiles.append({
                "p31_qid": p31_qid,
                "p31_label": p31_label,
                "unmod_count": unmod_count,
                "sample_size": sample_size,
                "pid": pid,
                "pid_label": plabel,
                "pid_count": cnt,
                "pid_pct": round(cnt / sample_size * 100, 1),
            })

    # Save property profiles to CSV
    if all_prop_profiles:
        con.execute("CREATE TABLE prop_profiles(p31_qid VARCHAR, p31_label VARCHAR, unmod_count BIGINT, sample_size BIGINT, pid VARCHAR, pid_label VARCHAR, pid_count BIGINT, pid_pct DOUBLE)")
        con.executemany(
            "INSERT INTO prop_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["p31_qid"], r["p31_label"], r["unmod_count"], r["sample_size"],
              r["pid"], r["pid_label"], r["pid_count"], r["pid_pct"])
             for r in all_prop_profiles]
        )
        con.execute(f"""
            COPY (SELECT * FROM prop_profiles ORDER BY unmod_count DESC, pid_pct DESC)
            TO '{args.output_dir}/p31_unmod_property_profiles.csv' (HEADER, DELIMITER ',')
        """)
        print(f"\n  -> Saved to {args.output_dir}/p31_unmod_property_profiles.csv")

    # Save coverage stats
    con.execute(f"""
        COPY (
            SELECT
                '{total}' AS total_entities,
                '{with_p31}' AS with_p31,
                '{with_modules}' AS with_modules,
                '{with_both}' AS with_both,
                '{no_modules}' AS no_modules,
                '{p31_no_mod}' AS p31_no_modules
        ) TO '{args.output_dir}/coverage_stats.csv' (HEADER, DELIMITER ',')
    """)

    con.close()
    print(f"\nAll results saved to {args.output_dir}/")


if __name__ == "__main__":
    main()
