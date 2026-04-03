#!/usr/bin/env python3
"""
Extract entities of a given category from classified parquet files into a single output file.

Usage:
    uv run --project src/python python src/python/extract_category.py --category science
    uv run --project src/python python src/python/extract_category.py --unclassified
    uv run --project src/python python src/python/extract_category.py --category people \
        --input-dir data_dump_core_output/classified_rust \
        --output data_dump_core_output/entity_people.parquet
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.parquet as pq


def extract(
    category: str | None,
    unclassified: bool,
    input_dir: Path,
    output: Path,
    batch_size: int = 65536,
) -> None:
    parquet_files = sorted(input_dir.glob("*.parquet"))
    if not parquet_files:
        print(f"No parquet files found in {input_dir}", file=sys.stderr)
        sys.exit(1)

    label = "unclassified" if unclassified else category
    print(f"Extracting: {label}")
    print(f"Input directory: {input_dir}")
    print(f"Output: {output}")
    print(f"Files to process: {len(parquet_files)}")

    output.parent.mkdir(parents=True, exist_ok=True)

    dataset = ds.dataset([str(f) for f in parquet_files], format="parquet")
    schema = dataset.schema

    if unclassified:
        filter_expr = ds.field("category").is_null()
    else:
        filter_expr = ds.field("category") == category

    writer = pq.ParquetWriter(str(output), schema, compression="zstd")
    total_read = 0
    total_written = 0

    t0 = time.time()
    for batch in dataset.to_batches(batch_size=batch_size, filter=filter_expr):
        total_read += len(batch)
        if len(batch) > 0:
            total_written += len(batch)
            writer.write_batch(batch)

    writer.close()
    elapsed = time.time() - t0

    print(f"\nTotal rows scanned (matching): {total_read}")
    print(f"{label} rows written: {total_written}")
    print(f"Output: {output}")
    print(f"Time: {elapsed:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract entities of a given category from classified parquet files"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--category", help="Category to extract (e.g. science, places)")
    group.add_argument(
        "--unclassified",
        action="store_true",
        help="Extract unclassified entities (category IS NULL)",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data_dump_output/classified_rust"),
        help="Input directory with classified parquet files",
    )
    parser.add_argument("--output", type=Path, help="Output parquet file")
    args = parser.parse_args()

    label = "unclassified" if args.unclassified else args.category
    output = args.output or Path(f"data_dump_output/entity_{label}.parquet")

    extract(args.category, args.unclassified, args.input_dir, output)


if __name__ == "__main__":
    main()
