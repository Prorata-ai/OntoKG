"""Download and reconstruct the CleanCoNLL dataset.

CleanCoNLL is a corrected version of CoNLL-03 NER with improved labels and
Wikipedia entity linking annotations (Rucker & Akbik, EMNLP 2023).

The repository distributes masked annotations ([TOKEN] placeholders) and patch
files. Reconstruction requires the original CoNLL-03 corpus, which the upstream
script downloads automatically.

Steps:
  1. Clone the CleanCoNLL repo
  2. Run the upstream reconstruction script
  3. Copy the resulting files into data/

Requires: git, bash, wget (used by upstream script)

Usage:
    python download_dataset.py
"""

import subprocess
import shutil
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
REPO_URL = "https://github.com/flairNLP/CleanCoNLL.git"
CLONE_DIR = SCRIPT_DIR / "_cleanconll_repo"

OUTPUT_FILES = [
    "cleanconll.train",
    "cleanconll.dev",
    "cleanconll.test",
]


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already reconstructed
    if all((DATA_DIR / f).exists() for f in OUTPUT_FILES):
        print("CleanCoNLL files already exist in data/. Delete them to re-download.")
        return

    # Clone repo
    if CLONE_DIR.exists():
        print(f"Repo already cloned at {CLONE_DIR}")
    else:
        print(f"Cloning {REPO_URL} ...")
        subprocess.run(["git", "clone", REPO_URL, str(CLONE_DIR)], check=True)

    # Run reconstruction script
    script = CLONE_DIR / "create_cleanconll_from_conll03.sh"
    if not script.exists():
        raise FileNotFoundError(f"Reconstruction script not found: {script}")

    print("Running reconstruction script (downloads CoNLL-03 + applies patches) ...")
    subprocess.run(["bash", str(script)], cwd=str(CLONE_DIR), check=True)

    # Copy output files
    src_dir = CLONE_DIR / "data" / "cleanconll"
    file_map = {
        "cleanconll.train": "cleanconll.train",
        "cleanconll.dev": "cleanconll.dev",
        "cleanconll.test": "cleanconll.test",
    }

    for src_name, dst_name in file_map.items():
        src_path = src_dir / src_name
        dst_path = DATA_DIR / dst_name
        if src_path.exists():
            shutil.copy2(src_path, dst_path)
            lines = sum(1 for _ in open(dst_path))
            print(f"  {dst_name}: {lines:,} lines")
        else:
            print(f"  WARNING: {src_path} not found")

    # Cleanup clone
    print(f"Cleaning up {CLONE_DIR} ...")
    shutil.rmtree(CLONE_DIR)

    print("\nDone. Files in data/:")
    for f in sorted(DATA_DIR.iterdir()):
        print(f"  {f.name}")


if __name__ == "__main__":
    main()
