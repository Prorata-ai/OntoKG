"""Resolve CleanCoNLL Wikipedia page titles to Wikidata QIDs with P31/P279.

Output: data/cleanconll_entities.tsv

Usage:
    python resolve_entities.py [--no-fetch]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
OUTPUT_PATH = DATA_DIR / "cleanconll_entities.tsv"

SPLITS = ["cleanconll.train", "cleanconll.dev", "cleanconll.test"]

sys.path.insert(0, str(SCRIPT_DIR.parent))
from resolve_common import resolve_titles


def extract_titles() -> set[str]:
    """Extract unique Wikipedia page titles from all CleanCoNLL splits."""
    titles: set[str] = set()
    for split in SPLITS:
        path = DATA_DIR / split
        if not path.exists():
            print(f"  WARNING: {path} not found, skipping")
            continue
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("-DOCSTART-") or not line:
                    continue
                cols = line.split("\t")
                if len(cols) < 3:
                    continue
                wiki = cols[2]
                if wiki != "O":
                    title = wiki[2:] if wiki[:2] in ("B-", "I-") else wiki
                    # Strip control characters (CleanCoNLL data artifact)
                    title = title.lstrip("\x00\x01\x02\x03\x04\x05\x06\x07\x08")
                    if title:
                        titles.add(title)
    return titles


def main(skip_fetch: bool = False) -> None:
    print("Extracting Wikipedia titles from CleanCoNLL …")
    titles = extract_titles()
    resolve_titles(titles, OUTPUT_PATH, skip_fetch)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip Wikidata API calls; use only cached/local data")
    args = parser.parse_args()
    main(skip_fetch=args.no_fetch)
