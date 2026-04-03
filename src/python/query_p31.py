#!/usr/bin/env python3
"""Query Wikidata for entities that have a given QID as their P31 (instance of) value."""

import argparse
import json
import urllib.parse
import urllib.request
import sys


def get_entity_info(qid: str) -> dict:
    """Get label and description for a QID."""
    url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&languages=en&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "WikidataResearch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode())

    entity = data.get("entities", {}).get(qid, {})
    return {
        "label": entity.get("labels", {}).get("en", {}).get("value", "N/A"),
        "description": entity.get("descriptions", {}).get("en", {}).get("value", "N/A"),
    }


def sparql_query(query: str) -> dict:
    """Execute a SPARQL query against Wikidata."""
    encoded = urllib.parse.quote(query)
    url = f"https://query.wikidata.org/sparql?query={encoded}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "WikidataResearch/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode())


def get_p31_count(qid: str) -> int:
    """Count entities with P31 = qid."""
    query = f"SELECT (COUNT(?item) as ?count) WHERE {{ ?item wdt:P31 wd:{qid} . }}"
    data = sparql_query(query)
    return int(data["results"]["bindings"][0]["count"]["value"])


def get_p31_samples(qid: str, limit: int = 50) -> list:
    """Get sample entities with P31 = qid."""
    query = f'''SELECT ?item ?itemLabel WHERE {{
  ?item wdt:P31 wd:{qid} .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}} LIMIT {limit}'''
    data = sparql_query(query)
    results = []
    for binding in data["results"]["bindings"]:
        item_qid = binding["item"]["value"].split("/")[-1]
        label = binding["itemLabel"]["value"]
        results.append((item_qid, label))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Query Wikidata for entities that have a given QID as their P31 (instance of) value"
    )
    parser.add_argument("qids", nargs="+", help="One or more QIDs to query (e.g., Q8436 Q6881511)")
    parser.add_argument("--samples", type=int, default=20, help="Number of sample entities to show (default: 20)")
    parser.add_argument("--no-samples", action="store_true", help="Skip fetching sample entities")
    parser.add_argument("--count-only", action="store_true", help="Only show label, description, and count")
    args = parser.parse_args()

    for qid in args.qids:
        # Normalize QID format
        qid = qid.upper()
        if not qid.startswith("Q"):
            qid = "Q" + qid

        print(f"\n{'='*60}")
        print(f"QID: {qid}")
        print("=" * 60)

        try:
            # Get entity info
            info = get_entity_info(qid)
            print(f"Label: {info['label']}")
            print(f"Description: {info['description']}")

            # Get P31 count
            count = get_p31_count(qid)
            print(f"Entities with P31 = {qid}: {count:,}")

            if args.count_only or args.no_samples:
                continue

            if count == 0:
                print("\nNo entities found with this P31 value.")
                continue

            # Get samples
            samples = get_p31_samples(qid, args.samples)
            print(f"\nSample entities (first {len(samples)}):")
            for sample_qid, label in samples:
                print(f"  {sample_qid}: {label}")

        except urllib.error.HTTPError as e:
            print(f"Error: HTTP {e.code} - {e.reason}", file=sys.stderr)
        except urllib.error.URLError as e:
            print(f"Error: {e.reason}", file=sys.stderr)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
