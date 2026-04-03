"""LLM extraction test: use YAML-driven prompt on bundled Wikipedia extracts."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from openai import AsyncOpenAI

client = AsyncOpenAI()
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "wiki_extracts.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data_demos"
DEFAULT_MODELS = ["gpt-5-nano"]
DEFAULT_SERVICE_TIER = "default"
DEFAULT_CONCURRENCY = 26

SYSTEM_PROMPT = """You are an entity extraction system. Given a text passage, extract all named entities and classify each one into a category, then assign relevant tags.

### Entity Categories

Use the following taxonomy to classify each extracted entity:

| Category | Entity Types (examples) |
|----------|------------------------|
| **Person** | human (real, living or historical people only — NOT mythological or fictional figures) |
| **Organization** | business, company, organization, nonprofit organization, university, government agency, political party, government organization, international organization, primary school |
| **Work** | book, novel, literary work, play, film, television series, mythological figure, deity, fictional character (entities from mythology or fiction that appear as characters in narratives) |
| **Place** | street, river, hill, human settlement, lake, village, stream, watercourse, valley, road (geographic locations only — NOT celestial objects) |
| **Science** | planet, asteroid, star, constellation, fossil taxon, galaxy, quasar, chemical element, chemical compound, mineral, disease, gene (includes all celestial/astronomical objects) |
| **Event** | event, action, occurrence, war, battle, invasion, conference, election, military operation, natural disaster |
| **Knowledge** | scholarly article, article, case report, editorial, meta-analysis, encyclopedia article, news article, theorem, algorithm, law, mythological concept, legendary creature (abstract concepts from mythology that are NOT specific characters) |
| **Product** | software as a service, aircraft, airplane, helicopter, vehicle, computer, software, video game, device, currency |

### Tags

For each category, assign tags from the list below if the tag is relevant to that entity **under the original context** — not based on general world knowledge.

- **Person**: entertainment, creativity, education, politics, sports, religion, finance, military, society, crime, family, award, healthcare, legal, culture, government, technology, food
- **Organization**: location, affiliation, education, international, government, corporation, culture, religion, politics, sports, broadcast, finance, society, healthcare, military, legal, transportation, entertainment, award, food
- **Work**: literature, film, television, art, comics, periodical, revenue, museum, music, sports, military, religion, character, culture, award, authorship, location, hierarchy
- **Place**: dwelling, commercial, culture, religion, infrastructure, transportation, government, administration, sports, heritage, education, nature, finance, military, entertainment, healthcare, affiliation, architecture, food
- **Science**: element, compound, mineral, geology, disease, gene, biofunction, protein, pharmaceutical, anatomy, organism, botany, finance, astronomy, clinical, healthcare, education, hierarchy, biosystem, clinical_trial, cell_lineage, genomics
- **Event**: military, conference, exhibition, disaster, violence, politics, award, sports, society, legal, entertainment, education, religion, transportation, hierarchy, route
- **Knowledge**: language, theorem, algorithm, specification, abstract, position, law, unit, field, phenomenon, publication, reference, name, religion, jurisdiction, education, culture, society, award, authorship
- **Product**: cloud, aircraft, vehicle, computer, software, game, device, military, food, tool, currency, religion, fiction, material, artifact, brand, entertainment, transportation, technology, award, authorship, location, hierarchy, operation

### Output Format

Return a JSON object with an "entities" key containing an array of extracted entities:

```json
{
  "entities": [
    {
      "entity": "Albert Einstein",
      "category": "Person",
      "tags": ["education", "award"],
      "context": "mentioned as Nobel Prize-winning physicist at Princeton"
    }
  ]
}
```

### Instructions

1. Extract all named entities from the text
2. Classify each into exactly one **category** from the taxonomy above
3. Assign **tags** only when the tag is relevant in the given context — do not tag based on general knowledge
4. Include the **context** field to explain why each tag was assigned
5. Include Wikidata QID if known; otherwise omit the field"""


MAX_RETRIES = 3


async def extract(entry: dict, model: str, tier: str) -> dict:
    text = entry["extract"]
    start = time.perf_counter()
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.with_options(timeout=900.0).chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"### TEXT:\n\n{text}"},
                ],
                service_tier=tier,
                response_format={"type": "json_object"},
            )
            break
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                elapsed = time.perf_counter() - start
                return {
                    "qid": entry["qid"],
                    "label": entry["label"],
                    "extract": text,
                    "extraction": {"entities": [], "error": str(e)},
                    "latency_s": round(elapsed, 2),
                }
    elapsed = time.perf_counter() - start
    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return {
        "qid": entry["qid"],
        "label": entry["label"],
        "extract": text,
        "extraction": parsed,
        "latency_s": round(elapsed, 2),
    }


async def run_model(
    model: str,
    tier: str,
    all_entries: list,
    concurrency: int,
    output_dir: Path,
) -> list:
    print(f"Model: {model} | Tier: {tier} | Workers: {concurrency} | Cases: {len(all_entries)}\n")

    semaphore = asyncio.Semaphore(concurrency)
    start_all = time.perf_counter()

    async def bounded_extract(entry):
        async with semaphore:
            return await extract(entry, model, tier)

    results = await asyncio.gather(*[bounded_extract(e) for e in all_entries])

    total_time = time.perf_counter() - start_all

    for r in results:
        n_entities = len(r["extraction"].get("entities", [])) if isinstance(r["extraction"], dict) else "?"
        print(f"  {r['qid']:12s} {r['label']:40s} {r['latency_s']:6.1f}s  {n_entities} entities")

    print(f"\nTotal: {len(results)} cases in {total_time:.1f}s\n")

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"extraction_{model}_{tier}.json"
    with out_path.open("w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Saved to {out_path}\n{'='*80}\n")
    return results


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Input JSON file with extract cases",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for model output JSON files",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        help="One or more model names to run",
    )
    parser.add_argument(
        "--service-tier",
        default=DEFAULT_SERVICE_TIER,
        help="OpenAI service tier to request",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Maximum number of concurrent requests",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit to the first N cases (0 = all)",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()

    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {input_path}. "
            f"Use --input to point at a JSON file of extract cases."
        )

    with input_path.open() as f:
        all_entries = json.load(f)

    if args.limit > 0:
        all_entries = all_entries[:args.limit]

    for model in args.models:
        await run_model(
            model=model,
            tier=args.service_tier,
            all_entries=all_entries,
            concurrency=args.concurrency,
            output_dir=output_dir,
        )


asyncio.run(main())
