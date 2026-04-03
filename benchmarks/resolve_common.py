"""Shared logic for resolving Wikipedia titles to Wikidata QIDs with P31/P279.

Checks a prebuilt classified parquet artifact first (covers ~96% of entities),
then falls back to the Wikidata API for the rest.

Note: ``data_dump_core_output/classified_with_sitelinks.parquet`` is expected to
come from the project's HuggingFace release artifacts. It is not emitted by the
default local 13-column classifier in this repo.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Optional prebuilt HuggingFace artifact used for benchmark fast-path lookup.
PARQUET_PATH = PROJECT_ROOT / "data_dump_core_output" / "classified_with_sitelinks.parquet"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "WikidataEntityResolver/1.0 (research)"


# ── Parquet resolution ───────────────────────────────────────────────────────

def _extract_p31_p279_from_claims(
    claims_json: str | None, qid_labels: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Extract P31/P279 QIDs and labels from classified parquet claims JSON."""
    p31: list[str] = []
    p279: list[str] = []
    if not claims_json:
        return p31, p279
    claims = json.loads(claims_json) if isinstance(claims_json, str) else claims_json
    for section in claims.values():
        if not isinstance(section, dict):
            continue
        for stmt in section.get("P31", {}).get("statements", []):
            val = stmt.get("value", "")
            if val.startswith("Q"):
                p31.append(val)
                if stmt.get("label"):
                    qid_labels[val] = stmt["label"]
        for stmt in section.get("P279", {}).get("statements", []):
            val = stmt.get("value", "")
            if val.startswith("Q"):
                p279.append(val)
                if stmt.get("label"):
                    qid_labels[val] = stmt["label"]
    return p31, p279


def resolve_from_parquet(titles: set[str]) -> tuple[dict, dict, set[str]]:
    """Look up titles in the classified parquet.

    Returns (entities, qid_labels, remaining_titles).
    entities: {title: {qid, label, description, p31, p279}}
    qid_labels: {qid: label} for P31/P279 target QIDs
    """
    entities: dict[str, dict] = {}
    qid_labels: dict[str, str] = {}

    if not PARQUET_PATH.exists():
        print(f"  Parquet not found at {PARQUET_PATH}, skipping local lookup")
        return entities, qid_labels, titles

    try:
        import duckdb
    except ImportError:
        print("  duckdb not installed, skipping local lookup")
        return entities, qid_labels, titles

    title_list = sorted(titles)
    con = duckdb.connect()

    result = con.execute("""
        WITH lookup AS (SELECT unnest(?::VARCHAR[]) as title)
        SELECT p.title, p.qid, p.label, p.description, p.p31_values, p.claims
        FROM lookup l
        JOIN read_parquet(?) p ON l.title = p.title
    """, [title_list, str(PARQUET_PATH)]).fetchall()
    for title, qid, label, description, p31_values, claims_json in result:
        p31, p279 = _extract_p31_p279_from_claims(claims_json, qid_labels)
        entities[title] = {
            "qid": qid,
            "label": label or "",
            "description": description or "",
            "p31": p31 if p31 else list(p31_values or []),
            "p279": p279,
        }

    # Second pass: try URL-decoded titles for any remaining
    remaining = titles - set(entities.keys())
    url_encoded = {t: urllib.parse.unquote(t) for t in remaining if "%" in t}
    if url_encoded:
        decoded_list = sorted(set(url_encoded.values()))
        result2 = con.execute("""
            WITH lookup AS (SELECT unnest(?::VARCHAR[]) as title)
            SELECT p.title, p.qid, p.label, p.description, p.p31_values, p.claims
            FROM lookup l
            JOIN read_parquet(?) p ON l.title = p.title
        """, [decoded_list, str(PARQUET_PATH)]).fetchall()

        decoded_to_orig: dict[str, str] = {v: k for k, v in url_encoded.items()}
        for title, qid, label, description, p31_values, claims_json in result2:
            orig_title = decoded_to_orig.get(title, title)
            p31, p279 = _extract_p31_p279_from_claims(claims_json, qid_labels)
            entities[orig_title] = {
                "qid": qid,
                "label": label or "",
                "description": description or "",
                "p31": p31 if p31 else list(p31_values or []),
                "p279": p279,
            }

    remaining = titles - set(entities.keys())
    con.close()

    print(f"  Parquet matched    : {len(entities):,}")
    print(f"  Need API fallback  : {len(remaining):,}")
    return entities, qid_labels, remaining


# ── Wikidata API resolution ──────────────────────────────────────────────────

def _api_get(params: dict) -> dict | None:
    params["format"] = "json"
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    url = f"{WIKIDATA_API}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                wait = min(2 ** (attempt + 2), 120)
                print(f"    Rate limited, waiting {wait}s …")
                time.sleep(wait)
                continue
            raise
    return None


def _extract_claim_targets(entity_data: dict, prop: str) -> list[str]:
    qids: list[str] = []
    for claim in entity_data.get("claims", {}).get(prop, []):
        mainsnak = claim.get("mainsnak", {})
        datavalue = mainsnak.get("datavalue", {})
        if datavalue.get("type") == "wikibase-entityid":
            numeric_id = datavalue["value"].get("numeric-id")
            if numeric_id is not None:
                qids.append(f"Q{numeric_id}")
    return qids


def _resolve_wikipedia_redirects(titles: list[str]) -> dict[str, str]:
    """Resolve Wikipedia redirects: original_title → canonical_title."""
    redirected: dict[str, str] = {}
    batch_size = 50

    for i in range(0, len(titles), batch_size):
        batch = titles[i : i + batch_size]
        api_titles = [t.replace("_", " ") for t in batch]
        params = {
            "action": "query",
            "titles": "|".join(api_titles),
            "redirects": "1",
            "format": "json",
        }
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
        url = f"https://en.wikipedia.org/w/api.php?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue

        norm_map: dict[str, str] = {}
        for entry in data.get("query", {}).get("normalized", []):
            norm_map[entry["from"]] = entry["to"]
        redir_map: dict[str, str] = {}
        for entry in data.get("query", {}).get("redirects", []):
            redir_map[entry["from"]] = entry["to"]

        for orig_title in batch:
            api_title = orig_title.replace("_", " ")
            canonical = norm_map.get(api_title, api_title)
            canonical = redir_map.get(canonical, canonical)
            canonical_us = canonical.replace(" ", "_")
            if canonical_us != orig_title:
                redirected[orig_title] = canonical_us

        time.sleep(0.3)

    return redirected


def resolve_from_api(
    titles: set[str], skip_fetch: bool = False
) -> tuple[dict, set[str]]:
    """Resolve remaining titles via Wikidata API.

    Returns (entities, unresolved_titles).
    """
    entities: dict[str, dict] = {}
    unresolved: set[str] = set()

    if not titles:
        return entities, unresolved

    if skip_fetch:
        print(f"  --no-fetch: skipping {len(titles):,} titles")
        return entities, titles

    # Step 0: Resolve Wikipedia redirects first
    title_list = sorted(titles)
    print(f"  Resolving Wikipedia redirects …")
    redirects = _resolve_wikipedia_redirects(title_list)
    if redirects:
        print(f"  Found {len(redirects):,} redirects")

    # Use canonical titles for Wikidata lookup
    canonical_to_orig: dict[str, list[str]] = {}
    for orig in title_list:
        canonical = redirects.get(orig, orig)
        canonical_to_orig.setdefault(canonical, []).append(orig)
    canonical_titles = sorted(canonical_to_orig.keys())

    # Step 1: Resolve canonical titles → QIDs
    title_to_qid: dict[str, str | None] = {}
    batch_size = 25

    print(f"  Resolving {len(canonical_titles):,} canonical titles → QIDs (batches of {batch_size}) …")
    for i in range(0, len(canonical_titles), batch_size):
        batch = canonical_titles[i : i + batch_size]
        api_titles = [t.replace("_", " ") for t in batch]
        space_to_canon = {t.replace("_", " "): t for t in batch}

        data = _api_get({
            "action": "wbgetentities",
            "sites": "enwiki",
            "titles": "|".join(api_titles),
            "props": "info|sitelinks",
            "languages": "en",
        })
        if not data:
            for canon in batch:
                for orig in canonical_to_orig.get(canon, [canon]):
                    unresolved.add(orig)
            continue

        normalized = {}
        for entry in data.get("normalized", []):
            normalized[entry["to"]] = entry["from"]
        wd_redirects = {}
        for entry in data.get("redirects", []):
            wd_redirects[entry["to"]] = entry["from"]

        for qid_key, entity_data in data.get("entities", {}).items():
            if "missing" in entity_data:
                miss_title = entity_data.get("title", "")
                orig = normalized.get(miss_title, miss_title)
                canon = space_to_canon.get(orig, orig.replace(" ", "_"))
                title_to_qid[canon] = None
                continue

            site_title = (
                entity_data.get("sitelinks", {})
                .get("enwiki", {})
                .get("title", "")
            )
            api_title = wd_redirects.get(site_title, site_title)
            api_title = normalized.get(api_title, api_title)
            canon = space_to_canon.get(api_title, api_title.replace(" ", "_"))
            title_to_qid[canon] = qid_key

        time.sleep(0.5)

    # Step 2: Fetch entity details for resolved QIDs
    qids_to_fetch = sorted({q for q in title_to_qid.values() if q})
    qid_to_entity: dict[str, dict] = {}

    if qids_to_fetch:
        print(f"  Fetching details for {len(qids_to_fetch):,} QIDs …")
        for i in range(0, len(qids_to_fetch), 50):
            batch = qids_to_fetch[i : i + 50]
            data = _api_get({
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "labels|descriptions|claims",
                "languages": "en",
            })
            if not data:
                continue

            for qid_key, entity_data in data.get("entities", {}).items():
                if "missing" in entity_data:
                    continue
                qid_to_entity[qid_key] = {
                    "label": entity_data.get("labels", {}).get("en", {}).get("value", ""),
                    "description": entity_data.get("descriptions", {}).get("en", {}).get("value", ""),
                    "p31": _extract_claim_targets(entity_data, "P31"),
                    "p279": _extract_claim_targets(entity_data, "P279"),
                }
            time.sleep(0.5)

    # Build final entities dict, mapping back to original titles
    for canon, qid in title_to_qid.items():
        orig_titles = canonical_to_orig.get(canon, [canon])
        if not qid or qid not in qid_to_entity:
            unresolved.update(orig_titles)
            continue
        ent = qid_to_entity[qid]
        for orig in orig_titles:
            entities[orig] = {"qid": qid, **ent}

    # Titles not even in title_to_qid (API didn't return them)
    for title in titles:
        if title not in entities:
            unresolved.add(title)

    return entities, unresolved


def resolve_qid_labels(
    qids: list[str], existing_labels: dict[str, str], skip_fetch: bool = False
) -> dict[str, str]:
    """Fetch labels for P31/P279 target QIDs not already labeled."""
    uncached = [q for q in qids if q not in existing_labels]
    print(f"  Type QIDs to label : {len(qids):,}")
    print(f"  Already have       : {len(qids) - len(uncached):,}")
    print(f"  Need fetching      : {len(uncached):,}")

    if not uncached or skip_fetch:
        return existing_labels

    for i in range(0, len(uncached), 50):
        batch = uncached[i : i + 50]
        data = _api_get({
            "action": "wbgetentities",
            "ids": "|".join(batch),
            "props": "labels",
            "languages": "en",
        })
        if not data:
            continue
        for qid_key, entity_data in data.get("entities", {}).items():
            if "missing" in entity_data:
                existing_labels[qid_key] = ""
                continue
            existing_labels[qid_key] = (
                entity_data.get("labels", {}).get("en", {}).get("value", "")
            )
        time.sleep(0.5)

    return existing_labels


# ── Write output ─────────────────────────────────────────────────────────────

def write_output(
    output_path: Path,
    titles: set[str],
    entities: dict[str, dict],
    qid_labels: dict[str, str],
) -> None:
    resolved = 0
    unresolved = 0

    with open(output_path, "w") as f:
        f.write("wikipedia_title\tqid\tlabel\tdescription\t"
                "p31_qids\tp31_labels\tp279_qids\tp279_labels\n")

        for title in sorted(titles):
            ent = entities.get(title)
            if not ent:
                f.write(f"{title}\t\t\t\t\t\t\t\n")
                unresolved += 1
                continue

            p31 = ent.get("p31", [])
            p279 = ent.get("p279", [])
            p31_labels = [qid_labels.get(q, "") for q in p31]
            p279_labels = [qid_labels.get(q, "") for q in p279]

            f.write(
                f"{title}\t{ent['qid']}\t{ent.get('label', '')}\t"
                f"{ent.get('description', '')}\t"
                f"{';'.join(p31)}\t{';'.join(p31_labels)}\t"
                f"{';'.join(p279)}\t{';'.join(p279_labels)}\n"
            )
            resolved += 1

    print(f"\nWrote {output_path}")
    print(f"  Resolved: {resolved:,}")
    print(f"  Unresolved: {unresolved:,}")


# ── Main pipeline ────────────────────────────────────────────────────────────

def resolve_titles(
    titles: set[str],
    output_path: Path,
    skip_fetch: bool = False,
) -> None:
    """Full resolution pipeline: parquet → API → write TSV."""
    print(f"  {len(titles):,} unique titles")

    print("\nStep 1: Resolve from local parquet …")
    entities, qid_labels, remaining = resolve_from_parquet(titles)

    print(f"\nStep 2: Resolve {len(remaining):,} remaining via API …")
    api_entities, unresolved = resolve_from_api(remaining, skip_fetch)
    for title, ent in api_entities.items():
        if title not in entities:
            entities[title] = ent
    if unresolved:
        print(f"  Still unresolved   : {len(unresolved):,}")

    # Collect all P31/P279 target QIDs that need labels
    type_qids: set[str] = set()
    for ent in entities.values():
        type_qids.update(ent.get("p31", []))
        type_qids.update(ent.get("p279", []))

    print(f"\nStep 3: Resolve labels for {len(type_qids):,} type QIDs …")
    qid_labels = resolve_qid_labels(sorted(type_qids), qid_labels, skip_fetch)

    print("\nStep 4: Writing output …")
    write_output(output_path, titles, entities, qid_labels)
