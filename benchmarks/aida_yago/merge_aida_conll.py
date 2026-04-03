"""Merge AIDA-YAGO2 entity links with CoNLL-2003 NER types.

Reads:
  - benchmarks/aida_yago/data/AIDA-YAGO2-dataset.tsv  (tokens + B/I + entity links)
  - benchmarks/aida_yago/data/conll2003_{split}.parquet (tokens + NER types)

Writes:
  - benchmarks/aida_yago/data/aida_conll_merged.tsv

Output format (tab-separated):
  doc_id  split  sentence_id  token_idx  token  ner_tag  mention  yago_name  wikipedia_url  freebase_mid
"""

import pyarrow.parquet as pq
import re
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
NER_LABELS = ["O", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC", "B-MISC", "I-MISC"]

# AIDA doc numbering: 1-946 = train, 947-1162 = testa (validation), 1163-1393 = testb (test)
SPLIT_RANGES = [
    ("train", 1, 946),
    ("testa", 947, 1162),
    ("testb", 1163, 1393),
]


def parse_aida(path):
    """Parse AIDA-YAGO2-dataset.tsv into per-document token lists.

    Returns: dict[int, list[dict]] mapping global doc_id to list of token records.
    Each token record: {token, bio, mention, yago_name, wikipedia_url, freebase_mid}
    """
    docs = {}
    current_doc_id = None
    current_tokens = []

    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("-DOCSTART-"):
                # Save previous doc
                if current_doc_id is not None:
                    docs[current_doc_id] = current_tokens
                # Parse doc id: "-DOCSTART- (123 ...)" or "-DOCSTART- (947testa ...)"
                m = re.match(r"-DOCSTART- \((\d+)", line)
                current_doc_id = int(m.group(1))
                current_tokens = []
            elif line == "":
                # Sentence boundary — skip
                continue
            else:
                parts = line.split("\t")
                token = parts[0]
                if len(parts) == 1:
                    # Non-entity token
                    current_tokens.append({
                        "token": token,
                        "bio": "O",
                        "mention": "",
                        "yago_name": "",
                        "wikipedia_url": "",
                        "freebase_mid": "",
                    })
                elif len(parts) >= 2:
                    bio = parts[1]  # B or I
                    mention = parts[2] if len(parts) > 2 else ""
                    yago_name = parts[3] if len(parts) > 3 else "--NME--"
                    wikipedia_url = parts[4] if len(parts) > 4 else ""
                    freebase_mid = parts[5] if len(parts) > 5 else ""
                    if yago_name == "--NME--":
                        yago_name = ""
                        wikipedia_url = ""
                        freebase_mid = ""
                    current_tokens.append({
                        "token": token,
                        "bio": bio,
                        "mention": mention,
                        "yago_name": yago_name,
                        "wikipedia_url": wikipedia_url,
                        "freebase_mid": freebase_mid,
                    })

    # Save last doc
    if current_doc_id is not None:
        docs[current_doc_id] = current_tokens

    return docs


def load_conll_split(split_name):
    """Load a CoNLL-2003 parquet split.

    Returns: dict[int, list[tuple[list[str], list[int], int]]]
        mapping per-split doc_id to list of (tokens, ner_tags, sentence_id).
    """
    path = DATA_DIR / f"conll2003_{split_name}.parquet"
    t = pq.read_table(str(path))
    docs = {}
    for i in range(len(t)):
        doc_id = t.column("document_id")[i].as_py()
        sent_id = t.column("sentence_id")[i].as_py()
        tokens = t.column("tokens")[i].as_py()
        ner_tags = t.column("ner_tags")[i].as_py()
        docs.setdefault(doc_id, []).append((tokens, ner_tags, sent_id))
    return docs


def merge():
    aida_docs = parse_aida(DATA_DIR / "AIDA-YAGO2-dataset.tsv")
    print(f"Parsed {len(aida_docs)} AIDA documents")

    conll_splits = {
        "train": load_conll_split("train"),
        "testa": load_conll_split("validation"),
        "testb": load_conll_split("test"),
    }

    out_path = DATA_DIR / "aida_conll_merged.tsv"
    total_tokens = 0
    total_entities = 0
    mismatches = 0

    with open(out_path, "w") as out:
        out.write("doc_id\tsplit\tsentence_id\ttoken_idx\ttoken\tner_tag\tmention\tyago_name\twikipedia_url\tfreebase_mid\n")

        for split_name, start, end in SPLIT_RANGES:
            conll_docs = conll_splits[split_name]

            for global_doc_id in range(start, end + 1):
                local_doc_id = global_doc_id - start + 1
                aida_tokens = aida_docs.get(global_doc_id, [])
                conll_sents = conll_docs.get(local_doc_id, [])

                # Flatten CoNLL sentences into token list with sentence IDs
                conll_flat = []
                for tokens, ner_tags, sent_id in conll_sents:
                    for tok, tag in zip(tokens, ner_tags):
                        conll_flat.append((tok, NER_LABELS[tag], sent_id))

                if len(aida_tokens) != len(conll_flat):
                    mismatches += 1
                    if mismatches <= 5:
                        print(f"  Length mismatch doc {global_doc_id}: AIDA={len(aida_tokens)} CoNLL={len(conll_flat)}")

                # Merge by position
                n = min(len(aida_tokens), len(conll_flat))
                for i in range(n):
                    aida = aida_tokens[i]
                    conll_tok, ner_tag, sent_id = conll_flat[i]

                    if aida["token"] != conll_tok:
                        # Token mismatch — keep both but flag
                        if mismatches <= 5:
                            print(f"  Token mismatch doc {global_doc_id} pos {i}: AIDA='{aida['token']}' CoNLL='{conll_tok}'")

                    out.write(f"{global_doc_id}\t{split_name}\t{sent_id}\t{i}\t{conll_tok}\t{ner_tag}\t{aida['mention']}\t{aida['yago_name']}\t{aida['wikipedia_url']}\t{aida['freebase_mid']}\n")
                    total_tokens += 1
                    if aida["yago_name"]:
                        total_entities += 1

    print(f"\nWrote {out_path}")
    print(f"  {total_tokens:,} tokens, {total_entities:,} linked entities")
    print(f"  {mismatches} documents with length mismatches")


if __name__ == "__main__":
    merge()
