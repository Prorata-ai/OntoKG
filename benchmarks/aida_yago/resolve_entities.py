"""Resolve AIDA-YAGO Wikipedia page titles to Wikidata QIDs with P31/P279.

Extracts unique Wikipedia titles from aida_conll_merged.tsv, resolves them
via local parquet first, then Wikidata API fallback.

Output: data/aida_entities.tsv

Usage:
    python resolve_entities.py [--no-fetch]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_PATH = DATA_DIR / "aida_conll_merged.tsv"
OUTPUT_PATH = DATA_DIR / "aida_entities.tsv"

sys.path.insert(0, str(SCRIPT_DIR.parent))
from resolve_common import resolve_titles


def extract_titles() -> set[str]:
    """Extract unique Wikipedia page titles from AIDA-YAGO merged TSV."""
    titles: set[str] = set()
    with open(INPUT_PATH) as f:
        next(f)  # skip header
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 9:
                url = cols[8].strip()
                if url and "/wiki/" in url:
                    title = url.rsplit("/wiki/", 1)[-1]
                    if title:
                        titles.add(title)
    return titles


def main(skip_fetch: bool = False) -> None:
    print("Extracting Wikipedia titles from AIDA-YAGO …")
    titles = extract_titles()
    resolve_titles(titles, OUTPUT_PATH, skip_fetch)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip Wikidata API calls; use only cached/local data")
    args = parser.parse_args()
    main(skip_fetch=args.no_fetch)
