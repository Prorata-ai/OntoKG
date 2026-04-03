"""Convert label lookup table between LMDB and Parquet formats.

LMDB is fast but not portable across OS/architecture. Parquet is portable
and compact, suitable for sharing on HuggingFace.

Usage:
  # Export LMDB -> Parquet
  uv run --project src/python python src/python/convert_labels.py --export

  # Import Parquet -> LMDB
  uv run --project src/python python src/python/convert_labels.py --import

  # Custom paths
  uv run --project src/python python src/python/convert_labels.py --export \
    --lmdb data_dump_output/labels_en.lmdb --parquet data_dump_output/labels_en.parquet
"""

import argparse
import os
import shutil
import tempfile
import time

import lmdb
import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_LMDB = os.path.join(os.path.dirname(__file__), "..", "..", "data_dump_output", "labels_en.lmdb")
DEFAULT_PARQUET = os.path.join(os.path.dirname(__file__), "..", "..", "data_dump_output", "labels_en.parquet")
LMDB_DB_NAME = "labels_en"
BATCH_SIZE = 500_000


def export_lmdb_to_parquet(lmdb_path: str, parquet_path: str) -> None:
    """Export all LMDB key-value pairs to a two-column Parquet file."""
    env = lmdb.open(lmdb_path, readonly=True, lock=False, max_dbs=1)
    dbi = env.open_db(LMDB_DB_NAME.encode())

    ids: list[str] = []
    labels: list[str] = []
    schema = pa.schema([
        ("id", pa.string(), False),
        ("label", pa.string(), False),
    ])
    total = 0
    start = time.time()

    tmp_path = parquet_path + ".tmp"
    writer = pq.ParquetWriter(tmp_path, schema, compression="zstd")

    with env.begin() as txn:
        cursor = txn.cursor(db=dbi)
        for key, value in cursor:
            ids.append(key.decode("utf-8"))
            labels.append(value.decode("utf-8"))
            if len(ids) >= BATCH_SIZE:
                table = pa.table({"id": ids, "label": labels}, schema=schema)
                writer.write_table(table)
                total += len(ids)
                elapsed = time.time() - start
                rate = total / elapsed if elapsed > 0 else 0
                print(f"  {total:,} entries written ({rate:,.0f}/s)")
                ids.clear()
                labels.clear()

    if ids:
        table = pa.table({"id": ids, "label": labels}, schema=schema)
        writer.write_table(table)
        total += len(ids)

    writer.close()
    env.close()

    if total == 0:
        os.unlink(tmp_path)
        print("LMDB is empty, no Parquet file created.")
        return

    os.replace(tmp_path, parquet_path)
    elapsed = time.time() - start
    size_mb = os.path.getsize(parquet_path) / 1e6
    print(f"Exported {total:,} entries to {parquet_path} ({size_mb:.1f} MB) in {elapsed:.1f}s")


def import_parquet_to_lmdb(parquet_path: str, lmdb_path: str) -> None:
    """Import a two-column Parquet file into a fresh LMDB."""
    pf = pq.ParquetFile(parquet_path)
    total_rows = pf.metadata.num_rows

    # Compute actual data size for map_size estimate
    byte_size = sum(rg.total_byte_size for rg in [pf.metadata.row_group(i) for i in range(pf.metadata.num_row_groups)])
    map_size = max(byte_size * 3, total_rows * 200)

    # Write to temp dir, then move atomically
    tmp_dir = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(lmdb_path)))
    try:
        env = lmdb.open(tmp_dir, map_size=map_size, max_dbs=1)
        dbi = env.open_db(LMDB_DB_NAME.encode())

        total = 0
        start = time.time()

        for batch in pf.iter_batches(batch_size=BATCH_SIZE, columns=["id", "label"]):
            ids = batch.column("id")
            labels_col = batch.column("label")
            with env.begin(write=True) as txn:
                for i in range(len(batch)):
                    eid = ids[i].as_py()
                    label = labels_col[i].as_py()
                    if eid is None or label is None:
                        continue
                    txn.put(eid.encode("utf-8"), label.encode("utf-8"), db=dbi)
            total += len(batch)
            elapsed = time.time() - start
            rate = total / elapsed if elapsed > 0 else 0
            print(f"  {total:,} / {total_rows:,} entries imported ({rate:,.0f}/s)")

        env.close()

        # Atomic replace
        if os.path.exists(lmdb_path):
            shutil.rmtree(lmdb_path)
        os.rename(tmp_dir, lmdb_path)

        elapsed = time.time() - start
        print(f"Imported {total:,} entries to {lmdb_path} in {elapsed:.1f}s")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert labels between LMDB and Parquet")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", action="store_true", help="LMDB -> Parquet")
    group.add_argument("--import", dest="import_", action="store_true", help="Parquet -> LMDB")
    parser.add_argument("--lmdb", default=DEFAULT_LMDB, help="LMDB directory path")
    parser.add_argument("--parquet", default=DEFAULT_PARQUET, help="Parquet file path")
    args = parser.parse_args()

    if args.export:
        print("Exporting LMDB -> Parquet")
        print(f"  LMDB:    {args.lmdb}")
        print(f"  Parquet: {args.parquet}")
        export_lmdb_to_parquet(args.lmdb, args.parquet)
    else:
        print("Importing Parquet -> LMDB")
        print(f"  Parquet: {args.parquet}")
        print(f"  LMDB:    {args.lmdb}")
        import_parquet_to_lmdb(args.parquet, args.lmdb)


if __name__ == "__main__":
    main()
