#!/usr/bin/env python3
"""Benchmark for tag classification performance using real sample data."""

import json
import time
from pathlib import Path
from tag_loader import TagLoader


def parse_wikidata_entity(entity: dict) -> dict:
    """Parse a Wikidata entity into simplified format."""
    qid = entity.get("id", "")
    properties = {}

    claims = entity.get("claims", {})
    for pid, claim_list in claims.items():
        values = []
        for claim in claim_list:
            mainsnak = claim.get("mainsnak", {})
            datavalue = mainsnak.get("datavalue", {})
            value_data = datavalue.get("value")

            if value_data is None:
                continue

            if isinstance(value_data, dict):
                if "id" in value_data:
                    values.append(value_data["id"])
                elif "time" in value_data:
                    values.append(value_data["time"])
                elif "amount" in value_data:
                    values.append(value_data["amount"])
                elif "text" in value_data:
                    values.append(value_data["text"])
            elif isinstance(value_data, str):
                values.append(value_data)

        if values:
            properties[pid] = values

    return {"qid": qid, "properties": properties}


def load_all_entities(data_dir: Path) -> list:
    """Load all entities from sample files."""
    entities = []

    sample_files = [
        "wikidata_people_sample_100.json",
        "wikidata_organizations_sample_100.json",
        "wikidata_places_sample_100.json",
        "wikidata_events_sample_100.json",
        "wikidata_creative_works_sample_100.json",
        "wikidata_products_sample_100.json",
        "wikidata_knowledge_sample_100.json",
        "wikidata_science_sample_v1.json",
    ]

    for filename in sample_files:
        filepath = data_dir / filename
        if not filepath.exists():
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        raw_entities = data.get("entities", data)
        for qid, raw_entity in raw_entities.items():
            entity = parse_wikidata_entity(raw_entity)
            entity["qid"] = qid
            entities.append(entity)

    return entities


def benchmark_load(iterations: int = 10) -> float:
    """Benchmark tag loading."""
    start = time.perf_counter()
    for _ in range(iterations):
        loader = TagLoader()
    elapsed = time.perf_counter() - start
    return elapsed / iterations * 1000  # ms per iteration


def benchmark_classify(loader: TagLoader, entities: list, iterations: int = 3) -> tuple:
    """Benchmark classification."""
    total_classifications = 0

    start = time.perf_counter()
    for _ in range(iterations):
        for entity in entities:
            loader.classify(entity)
            total_classifications += 1
    elapsed = time.perf_counter() - start

    time_per_entity = elapsed / total_classifications * 1000000  # microseconds
    return time_per_entity, total_classifications, elapsed


def main():
    data_dir = Path(__file__).parent.parent.parent / "data"

    print("=" * 60)
    print("Python Benchmark (Real Sample Data)")
    print("=" * 60)

    # Load entities
    print("Loading entities from data folder...")
    entities = load_all_entities(data_dir)
    print(f"Loaded {len(entities)} entities")
    print()

    # Warmup
    loader = TagLoader()
    for entity in entities[:100]:
        loader.classify(entity)

    # Benchmark loading
    load_time = benchmark_load(10)
    print(f"Tag Loading:     {load_time:.3f} ms per load")

    # Benchmark classification
    classify_time, total, elapsed = benchmark_classify(loader, entities, iterations=3)
    print(f"Classification:  {classify_time:.3f} µs per entity")

    # Throughput
    throughput = total / elapsed
    print(f"Throughput:      {throughput:,.0f} classifications/sec")

    print()
    print(f"Total: {total:,} classifications in {elapsed*1000:.2f} ms")


if __name__ == "__main__":
    main()
