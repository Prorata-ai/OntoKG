"""Query QID -> English label LMDB store.

Usage:
  python query_lmdb.py Q42 Q31 Q1

Optional flags:
  --path PATH   LMDB directory (default: ../../data_dump_output/labels_en.lmdb)
  --db NAME     LMDB database name (default: labels_en)
"""

import argparse
import os

import lmdb


def main() -> None:
    parser = argparse.ArgumentParser(description="Query LMDB for QID labels")
    parser.add_argument("qids", nargs="+", help="QIDs to lookup, e.g., Q42")
    parser.add_argument(
        "--path",
        default=os.path.join(os.path.dirname(__file__), "..", "..", "data_dump_output", "labels_en.lmdb"),
        help="LMDB directory path",
    )
    parser.add_argument("--db", default="labels_en", help="LMDB database name")
    args = parser.parse_args()

    env = lmdb.open(args.path, readonly=True, lock=False, max_dbs=1)
    dbi = env.open_db(args.db.encode())
    with env.begin() as txn:
        for qid in args.qids:
            val = txn.get(qid.encode("utf-8"), db=dbi)
            if val is None:
                print(f"{qid}\t<not found>")
            else:
                print(f"{qid}\t{val.decode('utf-8')}")


if __name__ == "__main__":
    main()
