"""
Classify Wikidata JSONL.zst dump into categorized Parquet files.

Claims are organized into core/intrinsic/relational/unclaimed buckets with labels
resolved, values simplified, and human-readable sentences generated.

Usage:
    uv run --project src/python python src/python/classify_dump.py \
        --input data_dump_core/wikidata-20260119-all_core_0002.jsonl.zst \
        --output data_dump_core_output/classified_python/0002.parquet

    # Test mode: process first N entities, output as JSONL
    uv run --project src/python python src/python/classify_dump.py \
        --input data_dump_core/wikidata-20260119-all_core_0002.jsonl.zst \
        --output data_dump_core_output/classified_python/0002.parquet \
        --test 10
"""

import argparse
import json
import os
import time

import lmdb
import pyarrow as pa
import pyarrow.parquet as pq
import zstandard as zstd

from tag_loader import TagLoader, extract_qid_values

SCHEMA = pa.schema([
    ("qid", pa.string()),
    ("label", pa.string()),
    ("description", pa.string()),
    ("aliases", pa.string()),       # JSON array
    ("p31_values", pa.list_(pa.string())),
    ("category", pa.string()),
    ("intrinsic_modules", pa.list_(pa.string())),
    ("relational_modules", pa.list_(pa.string())),
    ("gate_matched", pa.string()),
    ("gate_property", pa.string()),
    ("matched_indicators", pa.string()),  # JSON object
    ("claims", pa.string()),              # Full JSON
    ("sitelinks", pa.string()),           # Full JSON
])


class LabelCache:
    """In-memory cache over LMDB label lookups."""

    def __init__(self, txn, dbi):
        self._txn = txn
        self._dbi = dbi
        self._cache: dict[str, str | None] = {}

    def get(self, qid: str) -> str | None:
        try:
            return self._cache[qid]
        except KeyError:
            val = self._txn.get(qid.encode("utf-8"), db=self._dbi)
            label = val.decode("utf-8") if val else None
            self._cache[qid] = label
            return label


class CategoryConfig:
    """Pre-computed PID sets for a category's core and modules."""

    def __init__(self, cat_config: dict):
        self.core_pids: set[str] = set()
        core = cat_config.get("core", {})
        if isinstance(core, dict):
            if "props" in core:
                self.core_pids = set(core["props"])
            else:
                for v in core.values():
                    if isinstance(v, list):
                        self.core_pids.update(v)

        # module_name -> set of PIDs (indicators + value_props)
        self._module_pids: dict[str, set[str]] = {}
        # module_name -> "structural" | "relational"
        self.module_types: dict[str, str] = {}
        for mod_name, mod_config in cat_config.get("modules", {}).items():
            pids = set(mod_config.get("indicators", {}).keys())
            pids.update(mod_config.get("value_props", []))
            self._module_pids[mod_name] = pids
            self.module_types[mod_name] = mod_config.get("type", "intrinsic")

    def get_module_pids(self, modules: list[str]) -> dict[str, set[str]]:
        """Return pid -> set of module names for the given matched modules."""
        result: dict[str, set[str]] = {}
        for mod_name in modules:
            for pid in self._module_pids.get(mod_name, ()):
                result.setdefault(pid, set()).add(mod_name)
        return result


def open_lmdb(lmdb_path: str) -> tuple:
    env = lmdb.open(lmdb_path, readonly=True, lock=False, max_dbs=1)
    dbi = env.open_db(b"labels_en")
    return env, dbi


LANG_FALLBACK = ["en", "en-us", "en-gb", "mul"]


def _resolve_lang(lang_dict: dict) -> str | None:
    for lang in LANG_FALLBACK:
        if lang in lang_dict:
            return lang_dict[lang].get("value")
    return None


def _simplify_value(datavalue: dict) -> str | None:
    dtype = datavalue.get("type")
    val = datavalue.get("value")
    if dtype == "wikibase-entityid":
        return val.get("id")
    elif dtype == "string":
        return val
    elif dtype == "time":
        t = val.get("time", "")
        precision = val.get("precision", 11)
        if t.startswith("+"):
            t = t[1:]
        if "T" in t:
            t = t.split("T")[0]
        if precision <= 9:
            t = t.split("-")[0]
        elif precision == 10:
            parts = t.split("-")
            t = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else t
        return t
    elif dtype == "globecoordinate":
        lat = round(val.get("latitude", 0), 6)
        lon = round(val.get("longitude", 0), 6)
        return f"{lat},{lon}"
    elif dtype == "quantity":
        amt = val.get("amount", "")
        if isinstance(amt, str) and amt.startswith("+"):
            amt = amt[1:]
        unit = val.get("unit", "")
        if unit and unit != "1" and "entity/" in unit:
            unit_qid = unit.split("entity/")[-1]
            return f"{amt} {unit_qid}"
        return amt
    elif dtype == "monolingualtext":
        return val.get("text")
    return str(val) if val else None


def _simplify_snak(snak: dict, labels: LabelCache) -> dict:
    if snak.get("snaktype") == "novalue":
        return {"value": None, "label": "none"}
    if snak.get("snaktype") == "somevalue":
        return {"value": None, "label": "unknown"}
    datavalue = snak.get("datavalue", {})
    value = _simplify_value(datavalue)
    result = {"value": value}
    dtype = datavalue.get("type")
    if dtype == "wikibase-entityid" and value:
        label = labels.get(value)
        if label:
            result["label"] = label
    elif dtype == "quantity":
        raw_val = datavalue.get("value", {})
        raw_unit = raw_val.get("unit", "")
        if "entity/" in raw_unit:
            unit_qid = raw_unit.split("entity/")[-1]
            unit_label = labels.get(unit_qid)
            if unit_label:
                amt = raw_val.get("amount", "")
                if isinstance(amt, str) and amt.startswith("+"):
                    amt = amt[1:]
                result["label"] = f"{amt} {unit_label}"
    return result


def _simplify_qualifiers(qualifiers: dict, labels: LabelCache) -> dict:
    result = {}
    for pid, snaks in qualifiers.items():
        pid_label = labels.get(pid)
        values = [_simplify_snak(s, labels) for s in snaks]
        entry = {"values": values}
        if pid_label:
            entry["label"] = pid_label
        result[pid] = entry
    return result


def _make_sentence(entity_label: str, prop_label: str, stmt_result: dict) -> str:
    value_display = stmt_result.get("label") or stmt_result.get("value") or "unknown"
    sentence = f"{entity_label}'s {prop_label} is {value_display}"
    qualifiers = stmt_result.get("qualifiers")
    if qualifiers:
        parts = []
        for _pid, q in qualifiers.items():
            q_label = q.get("label", _pid)
            q_values = [v.get("label") or v.get("value") or "unknown" for v in q.get("values", [])]
            parts.append(f"{q_label}: {', '.join(q_values)}")
        sentence += f" ({'; '.join(parts)})"
    return sentence + "."


def _simplify_statement(stmt: dict, labels: LabelCache, entity_label: str = "", prop_label: str = "") -> dict:
    snak_result = _simplify_snak(stmt.get("mainsnak", {}), labels)
    result = {
        "value": snak_result.get("value"),
        "rank": stmt.get("rank", "normal"),
    }
    if "label" in snak_result:
        result["label"] = snak_result["label"]
    qualifiers = stmt.get("qualifiers")
    if qualifiers:
        result["qualifiers"] = _simplify_qualifiers(qualifiers, labels)
    if entity_label and prop_label:
        datatype = stmt.get("mainsnak", {}).get("datatype", "")
        if datatype not in ("external-id", "url", "commonsMedia", "geo-shape", "tabular-data"):
            result["sentence"] = _make_sentence(entity_label, prop_label, result)
    return result


def _simplify_pid(pid: str, stmts: list, labels: LabelCache, entity_label: str) -> tuple[str, dict]:
    """Simplify all statements for a PID. Returns (pid_label, entry dict)."""
    pid_label = labels.get(pid) or pid
    entry = {"statements": [_simplify_statement(s, labels, entity_label, pid_label) for s in stmts]}
    if pid_label != pid:
        entry["label"] = pid_label
    return pid_label, entry


def organize_claims(claims: dict, category: str | None, modules: list[str],
                    cat_configs: dict[str, CategoryConfig], labels: LabelCache,
                    entity_label: str = "") -> dict:
    """Organize raw claims into core/intrinsic/relational/unclaimed buckets with labels."""
    result = {"core": {}, "intrinsic": {}, "relational": {}, "unclaimed": {}}

    if not category:
        for pid, stmts in claims.items():
            _, entry = _simplify_pid(pid, stmts, labels, entity_label)
            result["unclaimed"][pid] = entry
        return result

    cat_config = cat_configs[category]
    module_pids = cat_config.get_module_pids(modules)
    claimed_pids = set()

    for pid in sorted(cat_config.core_pids):
        if pid not in claims:
            continue
        claimed_pids.add(pid)
        _, entry = _simplify_pid(pid, claims[pid], labels, entity_label)
        result["core"][pid] = entry

    for pid, mod_names in sorted(module_pids.items()):
        if pid in claimed_pids or pid not in claims:
            continue
        claimed_pids.add(pid)
        _, entry = _simplify_pid(pid, claims[pid], labels, entity_label)
        mod_name = sorted(mod_names)[0]
        bucket = cat_config.module_types.get(mod_name, "intrinsic")
        result[bucket].setdefault(mod_name, {})
        result[bucket][mod_name][pid] = entry

    for pid, stmts in claims.items():
        if pid in claimed_pids:
            continue
        _, entry = _simplify_pid(pid, stmts, labels, entity_label)
        result["unclaimed"][pid] = entry

    return result


def process_entity(entity: dict, loader: TagLoader, lmdb_label: str,
                   labels: LabelCache, cat_configs: dict[str, CategoryConfig]) -> dict | None:
    """Process a single Wikidata entity into a row dict with organized claims."""
    qid = entity.get("id")
    if not qid:
        return None

    en_label = lmdb_label

    descriptions = entity.get("descriptions", {})
    en_desc = _resolve_lang(descriptions)

    aliases = entity.get("aliases", {})
    en_aliases = [a["value"] for a in aliases.get("en", [])] if "en" in aliases else []

    claims = entity.get("claims", {})

    p31_values = extract_qid_values(claims, "P31")

    classification = loader.classify_raw(claims)

    matched_ind = classification["matched_indicators"]
    matched_ind_json = (
        json.dumps(matched_ind, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if matched_ind else None
    )

    aliases_json = (
        json.dumps(en_aliases, separators=(",", ":"), ensure_ascii=False)
        if en_aliases else None
    )

    organized = organize_claims(
        claims, classification["category"], classification["modules"] or [],
        cat_configs, labels, entity_label=en_label,
    )
    claims_json = json.dumps(organized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    enwiki = (entity.get("sitelinks", {}).get("enwiki") or {}).get("title")
    sitelinks_json = (
        json.dumps({"enwiki": f"https://en.wikipedia.org/wiki/{enwiki.replace(' ', '_')}"},
                    separators=(",", ":"), ensure_ascii=False)
        if enwiki else None
    )

    # Split modules by type
    all_modules = classification["modules"] or []
    category = classification["category"]
    if category and all_modules:
        cat_config = cat_configs[category]
        intrinsic = [m for m in all_modules if cat_config.module_types.get(m) == "intrinsic"]
        relational = [m for m in all_modules if cat_config.module_types.get(m) == "relational"]
    else:
        intrinsic = []
        relational = []

    return {
        "qid": qid,
        "label": en_label,
        "description": en_desc,
        "aliases": aliases_json,
        "p31_values": p31_values if p31_values else None,
        "category": category,
        "intrinsic_modules": intrinsic,
        "relational_modules": relational,
        "gate_matched": classification["gate_matched"],
        "gate_property": classification["gate_property"],
        "matched_indicators": matched_ind_json,
        "claims": claims_json,
        "sitelinks": sitelinks_json,
    }


def write_batch(writer: pq.ParquetWriter, rows: list[dict]) -> None:
    arrays = {col: [] for col in SCHEMA.names}
    for row in rows:
        for col in SCHEMA.names:
            arrays[col].append(row[col])
    table = pa.table(arrays, schema=SCHEMA)
    writer.write_table(table)


def read_lines(input_path: str, limit: int | None = None):
    """Yield (line_number, raw_bytes) from a JSONL.zst file."""
    count = 0
    with open(input_path, "rb") as fh:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    count += 1
                    yield count, line
                    if limit and count >= limit:
                        return
            if buf.strip():
                count += 1
                yield count, buf
                if limit and count >= limit:
                    return


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify Wikidata JSONL.zst dump to Parquet")
    parser.add_argument("--input", required=True, help="Input JSONL.zst file")
    parser.add_argument("--output", required=True, help="Output Parquet file")
    parser.add_argument(
        "--lmdb-path",
        default=os.path.join("data_dump_output", "labels_en.lmdb"),
        help="LMDB label store path",
    )
    parser.add_argument("--tags-dir", default=None, help="YAML tags directory (default: src/tags)")
    parser.add_argument("--batch-size", type=int, default=10_000, help="Rows per parquet write batch")
    parser.add_argument("--test", type=int, nargs="?", const=10, metavar="N",
                        help="Test mode: process first N lines (default 10), output as JSONL")
    args = parser.parse_args()

    loader = TagLoader(tags_dir=args.tags_dir)
    print(f"Loaded {len(loader.categories)} categories: {', '.join(loader.get_category_names())}")

    env, dbi = open_lmdb(args.lmdb_path)
    txn = env.begin()
    labels = LabelCache(txn, dbi)

    # Pre-compute category configs
    cat_configs = {name: CategoryConfig(cfg) for name, cfg in loader.categories.items()}

    is_test = args.test is not None
    limit = args.test if is_test else None

    if is_test:
        output_path = args.output.rsplit(".", 1)[0] + ".jsonl"
    else:
        output_path = args.output

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    total = 0
    has_label = 0
    classified = 0
    start = time.time()

    writer = None
    out_file = None

    try:
        if is_test:
            out_file = open(output_path, "w")
        else:
            writer = pq.ParquetWriter(output_path, SCHEMA)

        batch: list[dict] = []

        for _n, raw_line in read_lines(args.input, limit=limit):
            total += 1
            entity = json.loads(raw_line)
            qid = entity.get("id", "")

            lmdb_label = labels.get(qid)
            if not lmdb_label:
                continue
            has_label += 1

            row = process_entity(entity, loader, lmdb_label, labels, cat_configs)
            if row is None:
                continue

            if row["category"] is not None:
                classified += 1

            if is_test:
                # Decode JSON strings back to objects for readable JSONL
                test_row = dict(row)
                for key in ("aliases", "matched_indicators", "claims", "sitelinks"):
                    if test_row[key]:
                        test_row[key] = json.loads(test_row[key])
                out_file.write(json.dumps(test_row, ensure_ascii=False) + "\n")
            else:
                batch.append(row)
                if len(batch) >= args.batch_size:
                    write_batch(writer, batch)
                    batch.clear()
                    elapsed = time.time() - start
                    rate = total / elapsed if elapsed > 0 else 0
                    print(
                        f"  {total:,} entities | {has_label:,} with label | "
                        f"{classified:,} classified | {rate:,.0f} ent/s"
                    )

        if not is_test and batch:
            write_batch(writer, batch)

    finally:
        if writer:
            writer.close()
        if out_file:
            out_file.close()
        txn.abort()
        env.close()

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Total entities: {total:,}")
    print(f"  With English label: {has_label:,}")
    if has_label:
        print(f"  Classified: {classified:,} ({classified/has_label*100:.1f}% of labeled)")
    print(f"  Output: {output_path}")
    print(f"  Label cache: {len(labels._cache):,} entries")


if __name__ == "__main__":
    main()
