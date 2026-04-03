#!/usr/bin/env python3
"""
Entity Disambiguation using YAML Module Fingerprints

Given an ambiguous entity name (e.g., "Mercury", "Victoria", "John Smith"),
fetches Wikidata candidates with exact label matches and classifies them
using the YAML tag definitions. The key insight: module assignments provide
rich semantic context beyond just category — two entities in the same category
(e.g., John Smith the botanist vs. the politician) are distinguished by their
module profiles.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

import lmdb

# Add src/python to path for tag_loader
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tag_loader import TagLoader, extract_qid_values

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
ENTITY_DATA_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
REQUEST_DELAY = 0.5
LMDB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data_dump_output", "labels_en.lmdb")
LMDB_DB_NAME = "labels_en"

# LMDB-backed label lookups
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
        print(f"  Warning: LMDB not available ({e})")
        return False


def lmdb_lookup(eid: str) -> str:
    """Look up English label for a QID/PID from LMDB."""
    if eid in _label_cache:
        return _label_cache[eid]
    if _init_lmdb() and _lmdb_env is not None:
        with _lmdb_env.begin() as txn:
            val = txn.get(eid.encode("utf-8"), db=_lmdb_dbi)
            if val is not None:
                label = val.decode("utf-8")
                _label_cache[eid] = label
                return label
    return ""


def api_get(url: str) -> dict:
    """Fetch JSON from a URL with a User-Agent header."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "WikidataPaperDemo/1.0 (research project)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_entities(query: str, limit: int = 10) -> list[dict]:
    """Search Wikidata for entities matching a query string."""
    params = urllib.parse.urlencode({
        "action": "wbsearchentities",
        "search": query,
        "language": "en",
        "format": "json",
        "limit": limit,
    })
    try:
        data = api_get(f"{WIKIDATA_API}?{params}")
        return data.get("search", [])
    except Exception as e:
        print(f"  Warning: search failed for \"{query}\": {e}")
        return []


def fetch_entity(qid: str) -> dict | None:
    """Fetch full entity data from Wikidata."""
    url = ENTITY_DATA_URL.format(qid=qid)
    try:
        data = api_get(url)
        entities = data.get("entities", {})
        return entities.get(qid)
    except Exception as e:
        print(f"  Warning: could not fetch {qid}: {e}")
        return None


def format_indicator_label(indicator: str) -> str:
    """Format an indicator string with labels from LMDB.

    For presence-based indicators like 'P345', returns 'IMDb ID'.
    For value-based indicators like 'P31:Q11424', returns 'instance of: film'.
    """
    if ":" in indicator:
        pid, qid = indicator.split(":", 1)
        pid_label = lmdb_lookup(pid)
        qid_label = lmdb_lookup(qid)
        if pid_label and qid_label:
            return f"{pid_label}: {qid_label}"
        elif pid_label:
            return pid_label
        elif qid_label:
            return qid_label
        return ""
    return lmdb_lookup(indicator)


def disambiguate(query: str, loader: TagLoader, limit: int = 10) -> list[dict]:
    """Disambiguate an entity name using YAML module fingerprints."""
    print(f"\nSearching Wikidata for: \"{query}\"")
    candidates = search_entities(query, limit=limit)
    # Keep only exact label matches (case-insensitive)
    exact = [c for c in candidates if c.get("label", "").lower() == query.lower()]
    print(f"  Found {len(candidates)} candidates, {len(exact)} exact matches")

    results = []
    for cand in exact:
        qid = cand["id"]
        label = cand.get("label", "")
        description = cand.get("description", "")
        print(f"  Fetching {qid} ({label})...")
        time.sleep(REQUEST_DELAY)

        entity = fetch_entity(qid)
        if not entity:
            continue

        claims = entity.get("claims", {})
        result = loader.classify_raw(claims)

        results.append({
            "qid": qid,
            "label": label,
            "description": description,
            "category": result["category"],
            "modules": result["modules"],
            "matched_indicators": result["matched_indicators"],
            "claims": claims,
            "total_properties": len(claims),
        })

    # Sort by number of modules (richer profiles first)
    results.sort(key=lambda x: len(x["modules"]), reverse=True)
    return results


def format_module_value_props(mod_name: str, cat_config: dict, claims: dict) -> str | None:
    """Format present value_props for a module as labeled PIDs."""
    mod_cfg = cat_config.get("modules", {}).get(mod_name, {})
    vps = mod_cfg.get("value_props", [])
    if not vps:
        return None
    present = [p for p in vps if p in claims]
    if not present:
        return None
    labeled = [f"`{p}` ({lmdb_lookup(p) or p})" for p in present]
    return ", ".join(labeled)


def format_results(query: str, results: list[dict], loader: TagLoader) -> str:
    """Format results as markdown with module-level detail."""
    lines = []
    lines.append(f"### \"{query}\"\n")

    for i, r in enumerate(results):
        desc_part = f" — {r['description']}" if r["description"] else ""
        lines.append(f"**{i+1}. {r['qid']} {r['label']}**{desc_part}")

        if r["category"]:
            lines.append(f"- **Category**: {r['category']}")
            cat_config = loader.categories.get(r["category"], {})
            claims = r.get("claims", {})

            if r["modules"]:
                intrinsic = []
                relational = []

                for mod in r["modules"]:
                    mod_cfg = cat_config.get("modules", {}).get(mod, {})
                    mod_type = mod_cfg.get("type", "?")
                    if mod_type == "intrinsic":
                        intrinsic.append(mod)
                    else:
                        relational.append(mod)

                if intrinsic:
                    lines.append("- **Intrinsic modules**:")
                    for mod in intrinsic:
                        vp_str = format_module_value_props(mod, cat_config, claims)
                        lines.append(f"  - **{mod}**" + (f": {vp_str}" if vp_str else ""))
                if relational:
                    lines.append("- **Relational modules**:")
                    for mod in relational:
                        vp_str = format_module_value_props(mod, cat_config, claims)
                        lines.append(f"  - **{mod}**" + (f": {vp_str}" if vp_str else ""))
            else:
                lines.append("- **Modules**: none matched")
        else:
            lines.append("- **Category**: *unclassified*")

        lines.append("")

    return "\n".join(lines)


def print_console(query: str, results: list[dict], loader: TagLoader):
    """Print detailed console output."""
    print(f"\n{'='*70}")
    print(f"DISAMBIGUATION: \"{query}\"")
    print(f"{'='*70}")

    for i, r in enumerate(results):
        print(f"\n--- #{i+1}: {r['qid']} — {r['label']} ---")
        print(f"  {r['description']}")

        if r["category"]:
            print(f"  Category: {r['category']}")
            cat_config = loader.categories.get(r["category"], {})
            claims = r.get("claims", {})

            if r["modules"]:
                intrinsic = []
                relational = []
                for mod in r["modules"]:
                    mod_cfg = cat_config.get("modules", {}).get(mod, {})
                    if mod_cfg.get("type") == "intrinsic":
                        intrinsic.append(mod)
                    else:
                        relational.append(mod)

                if intrinsic:
                    print(f"  Intrinsic modules:")
                    for mod in intrinsic:
                        vp_str = format_module_value_props(mod, cat_config, claims)
                        print(f"    {mod}" + (f" — {vp_str}" if vp_str else ""))
                if relational:
                    print(f"  Relational modules:")
                    for mod in relational:
                        vp_str = format_module_value_props(mod, cat_config, claims)
                        print(f"    {mod}" + (f" — {vp_str}" if vp_str else ""))
        else:
            print("  Category: UNCLASSIFIED")


def main():
    loader = TagLoader()
    _init_lmdb()  # Pre-initialize LMDB for fast lookups

    print(f"Loaded {len(loader.categories)} categories:")
    for cat in loader.categories:
        modules = loader.get_modules(cat)
        gate_count = len(loader.get_gate_qids(cat))
        print(f"  {cat}: {gate_count} gate QIDs, {len(modules)} modules")

    queries = {
        "Mercury": "Planet, element, Roman god, car brand, commune",
        "Victoria": "Queen, city, state, goddess, insect genus",
        "Georgia": "US state, country, given name, plant genus",
        "Phoenix": "City, constellation, plant genus, journal",
        "Cambridge": "Cities in England, Massachusetts, Ontario, Tasmania",
        "Michael Jordan": "Basketball player, artist, footballer, mycologist",
        "John Smith": "Botanist, politician, senator, philosopher",
    }

    all_md = []
    all_md.append("# Entity Disambiguation via YAML Module Fingerprints\n")
    all_md.append("Each entity's **module profile** — not just its category — provides a rich")
    all_md.append("semantic fingerprint. Two entities in the same category are distinguished by")
    all_md.append("which modules they activate and which indicators matched.\n")

    for query, note in queries.items():
        print(f"\n\n{'#'*70}")
        print(f"# {query} — {note}")
        print(f"{'#'*70}")

        results = disambiguate(query, loader, limit=10)
        print_console(query, results, loader)
        md = format_results(query, results, loader)
        all_md.append(md)

    return "\n".join(all_md)


if __name__ == "__main__":
    output = main()

    log_dir = Path(__file__).resolve().parent.parent.parent.parent / "logs" / "yaml_applications"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "02_entity_disambiguation.md"

    with open(log_path, "w") as f:
        f.write(output)
    print(f"\n\nMarkdown results saved to: {log_path}")
