"""
Property Organization Demo

Fetches entity data from Wikidata's REST API and demonstrates how the YAML
classification system transforms a flat bag of ~50-100+ raw properties into
a structured view organized by core properties, intrinsic modules, and
relational modules. Unclaimed properties are shown separately.

This is the "before vs after" demo: raw Wikidata → organized property graph.

Usage:
    uv run --project src/python python src/python/demos/property_organizer.py
    uv run --project src/python python src/python/demos/property_organizer.py Q42 Q312 Q9141
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import lmdb

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from tag_loader import TagLoader

WIKIDATA_API = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
USER_AGENT = "WikidataClassificationDemo/1.0 (research project)"
LMDB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data_dump_output", "labels_en.lmdb")
LMDB_DB_NAME = "labels_en"

# Entities chosen for property richness across different categories
DEFAULT_ENTITIES = [
    "Q76",      # Barack Obama — person with many modules
    "Q312",     # Apple Inc. — organization with tech, finance, legal
    "Q9141",    # Taj Mahal — place with heritage, architecture, religion
    "Q12418",   # Mona Lisa — artwork with authorship, location
    "Q53268",   # Toyota — organization (manufacturer)
]

_lmdb_env = None
_lmdb_dbi = None
_label_cache: dict[str, str] = {}


def _init_lmdb() -> bool:
    """Initialize LMDB connection for label lookups."""
    global _lmdb_env, _lmdb_dbi
    if _lmdb_env is not None:
        return True
    try:
        _lmdb_env = lmdb.open(LMDB_PATH, readonly=True, lock=False, max_dbs=1)
        _lmdb_dbi = _lmdb_env.open_db(LMDB_DB_NAME.encode())
        return True
    except Exception as e:
        print(f"  Warning: LMDB not available ({e}), falling back to API lookups")
        return False


def fetch_entity(qid: str) -> dict | None:
    """Fetch entity data from Wikidata REST API."""
    url = WIKIDATA_API.format(qid=qid)
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        entities = data.get("entities", {})
        return entities.get(qid)
    except (HTTPError, URLError, TimeoutError) as e:
        print(f"  ERROR fetching {qid}: {e}")
        return None


def get_english_label(entity: dict) -> str:
    """Extract English label from entity data."""
    labels = entity.get("labels", {})
    for lang in ("en", "en-us", "en-gb", "mul"):
        if lang in labels:
            return labels[lang]["value"]
    return "(no English label)"


def get_english_description(entity: dict) -> str:
    """Extract English description from entity data."""
    descs = entity.get("descriptions", {})
    for lang in ("en", "en-us", "en-gb", "mul"):
        if lang in descs:
            return descs[lang]["value"]
    return "(no description)"


def get_label(eid: str) -> str | None:
    """Look up English label for a QID/PID via LMDB. Returns None if unavailable."""
    if eid in _label_cache:
        return _label_cache[eid]
    if _init_lmdb() and _lmdb_env is not None:
        with _lmdb_env.begin() as txn:
            val = txn.get(eid.encode("utf-8"), db=_lmdb_dbi)
            if val is not None:
                label = val.decode("utf-8")
                _label_cache[eid] = label
                return label
    return None


def format_eid(eid: str) -> str:
    """Format an entity/property ID: 'QID (label)' if LMDB has a label, else just 'QID'."""
    label = get_label(eid)
    return f"{eid} ({label})" if label is not None else eid


def format_value(claim: dict) -> str:
    """Format a single claim's value to a readable string."""
    mainsnak = claim.get("mainsnak", {})
    if mainsnak.get("snaktype") != "value":
        return "(unknown value)"
    datavalue = mainsnak.get("datavalue", {})
    dtype = datavalue.get("type", "")
    value = datavalue.get("value", {})

    if dtype == "wikibase-entityid":
        qid = value.get("id", "?")
        return format_eid(qid)
    elif dtype == "string":
        s = datavalue.get("value", "")
        return s if len(s) <= 60 else s[:57] + "..."
    elif dtype == "time":
        return value.get("time", "?")
    elif dtype == "quantity":
        amount = value.get("amount", "?")
        unit = value.get("unit", "")
        if unit and unit != "1":
            unit_qid = unit.rsplit("/", 1)[-1] if "/" in unit else unit
            return f"{amount} {format_eid(unit_qid)}"
        return str(amount)
    elif dtype == "monolingualtext":
        return f'{value.get("text", "?")} [{value.get("language", "?")}]'
    elif dtype == "globecoordinate":
        return f'{value.get("latitude", "?")}, {value.get("longitude", "?")}'
    else:
        return str(datavalue.get("value", "?"))[:60]


def _claim_sort_key(claim: dict) -> str:
    """Extract a date sort key from claim qualifiers (P580 start, P582 end, P585 point in time).
    Returns the date string for descending sort (latest first), or empty string if no date."""
    for pid in ("P582", "P580", "P585"):
        quals = claim.get("qualifiers", {}).get(pid, [])
        for q in quals:
            if q.get("snaktype") == "value":
                t = q.get("datavalue", {}).get("value", {}).get("time", "")
                if t:
                    return t
    return ""


def format_values(claims_list: list[dict], max_vals: int = 3) -> str:
    """Format multiple claim values sorted latest-first, truncating to max_vals."""
    sorted_claims = sorted(claims_list, key=_claim_sort_key, reverse=True)
    vals = [format_value(c) for c in sorted_claims[:max_vals]]
    suffix = f" (+{len(claims_list) - max_vals} more)" if len(claims_list) > max_vals else ""
    return "; ".join(vals) + suffix


def organize_entity(loader: TagLoader, qid: str) -> dict:
    """Fetch, classify, and organize an entity's properties."""
    print(f"\n{'='*70}")
    print(f"  Entity: {format_eid(qid)}")
    print(f"{'='*70}")

    entity = fetch_entity(qid)
    if not entity:
        return {"qid": qid, "error": "Failed to fetch"}

    label = get_english_label(entity)
    description = get_english_description(entity)
    print(f"  Label:       {label}")
    print(f"  Description: {description}")

    claims = entity.get("claims", {})
    total_props = len(claims)

    # Classify
    result = loader.classify_raw(claims)
    category = result["category"]

    if not category:
        print(f"  UNCLASSIFIED ({total_props} raw properties)")
        return {
            "qid": qid, "label": label, "description": description,
            "category": None, "total_props": total_props,
            "raw_pids": sorted(claims.keys()),
        }

    cat_config = loader.categories[category]
    modules_config = cat_config.get("modules", {})

    # Build the organized view: assign each PID to a bucket
    # Core props
    core = cat_config.get("core", {})
    core_pids = set()
    if isinstance(core, dict):
        if "props" in core:
            core_pids = set(core["props"])
        else:
            for v in core.values():
                if isinstance(v, list):
                    core_pids.update(v)

    # Module props — derive from matched_indicators (exact matches only)
    intrinsic_map: dict[str, set[str]] = {}  # module -> PIDs
    relational_map: dict[str, set[str]] = {}  # module -> PIDs
    matched_modules = result.get("modules", [])
    matched_indicators = result.get("matched_indicators") or {}

    for mod_name in matched_modules:
        mod_cfg = modules_config.get(mod_name, {})
        mod_type = mod_cfg.get("type", "unknown")
        mod_pids = set()
        # Extract PIDs from matched indicators ("PID" or "PID:QID" format)
        for ind in matched_indicators.get(mod_name, []):
            pid = ind.split(":")[0]
            mod_pids.add(pid)
        # Value props that are present in claims
        for pid in mod_cfg.get("value_props", []):
            if pid in claims:
                mod_pids.add(pid)
        if mod_type == "intrinsic":
            intrinsic_map[mod_name] = mod_pids
        else:
            relational_map[mod_name] = mod_pids

    # Compute assigned vs unclaimed using unique PIDs
    assigned_pids = set(core_pids)
    for pids in intrinsic_map.values():
        assigned_pids |= pids
    for pids in relational_map.values():
        assigned_pids |= pids
    present_pids = set(claims.keys())
    unclaimed_pids = present_pids - assigned_pids
    present_core = core_pids & present_pids

    # --- Print organized view ---
    print(f"\n  Category: {category}")
    print(f"  Total raw properties: {total_props}")
    print(f"  Organized: {len(present_pids - unclaimed_pids)} assigned, "
          f"{len(unclaimed_pids)} unclaimed")

    # Core
    print(f"\n  ── Core ({len(present_core)}/{len(core_pids)}) ──")
    for pid in sorted(present_core):
        vals = format_values(claims[pid])
        print(f"    {format_eid(pid):40s} → {vals}")

    # Intrinsic modules
    if intrinsic_map:
        print(f"\n  ── Intrinsic Modules ({len(intrinsic_map)}) ──")
        for mod_name in sorted(intrinsic_map):
            pids = intrinsic_map[mod_name]
            present = pids & present_pids
            print(f"    [{mod_name}] ({len(present)} properties)")
            for pid in sorted(present):
                vals = format_values(claims[pid])
                print(f"      {format_eid(pid):40s} → {vals}")

    # Relational modules
    if relational_map:
        print(f"\n  ── Relational Modules ({len(relational_map)}) ──")
        for mod_name in sorted(relational_map):
            pids = relational_map[mod_name]
            present = pids & present_pids
            print(f"    [{mod_name}] ({len(present)} properties)")
            for pid in sorted(present):
                vals = format_values(claims[pid])
                print(f"      {format_eid(pid):40s} → {vals}")

    # Unclaimed
    if unclaimed_pids:
        print(f"\n  ── Unclaimed ({len(unclaimed_pids)}) ──")
        for pid in sorted(unclaimed_pids)[:15]:
            vals = format_values(claims[pid], max_vals=2)
            print(f"    {format_eid(pid):40s} → {vals}")
        if len(unclaimed_pids) > 15:
            print(f"    ... and {len(unclaimed_pids) - 15} more")

    # Build result for report
    return {
        "qid": qid,
        "label": label,
        "description": description,
        "category": category,
        "gate_matched": result["gate_matched"],
        "gate_property": result["gate_property"],
        "total_props": total_props,
        "core": {
            "defined": len(core_pids),
            "present": len(present_core),
            "pids": sorted(present_core),
        },
        "intrinsic": {
            mod: sorted(pids & present_pids)
            for mod, pids in sorted(intrinsic_map.items())
        },
        "relational": {
            mod: sorted(pids & present_pids)
            for mod, pids in sorted(relational_map.items())
        },
        "unclaimed": sorted(unclaimed_pids),
        "modules": matched_modules,
        "module_types": {
            m: modules_config.get(m, {}).get("type", "?") for m in matched_modules
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Property organization demo")
    parser.add_argument("entities", nargs="*", default=DEFAULT_ENTITIES,
                        help="QIDs to organize (default: diverse set of 6)")
    args = parser.parse_args()

    print("Loading YAML tag definitions...")
    loader = TagLoader()
    print(f"  Loaded {len(loader.categories)} categories: {', '.join(loader.get_category_names())}")

    results = []
    for qid in args.entities:
        result = organize_entity(loader, qid)
        results.append(result)
        time.sleep(0.5)

    # Summary
    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    for r in results:
        if r.get("error"):
            print(f"    {format_eid(r['qid']):40s} -> ERROR")
            continue
        if not r.get("category"):
            print(f"    {format_eid(r['qid']):40s} -> UNCLASSIFIED ({r['total_props']} props)")
            continue
        n_unclaimed = len(r["unclaimed"])
        assigned = r["total_props"] - n_unclaimed
        pct = round(100 * assigned / r["total_props"]) if r["total_props"] else 0
        mods = len(r["intrinsic"]) + len(r["relational"])
        print(f"    {format_eid(r['qid']):40s} -> {r['category']:30s} "
              f"{assigned}/{r['total_props']} organized ({pct}%), {mods} modules")


if __name__ == "__main__":
    main()
