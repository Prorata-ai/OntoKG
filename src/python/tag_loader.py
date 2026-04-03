"""
Tag Loader for Wikidata Entity Classification

Loads YAML tag configurations and classifies entities into categories and modules
based on their Wikidata properties.
"""

import os
from pathlib import Path
from typing import Any

import yaml


def extract_qid_values(claims: dict[str, list], pid: str) -> list[str]:
    """
    Extract QID values from raw Wikidata claims for a given property.

    Handles the nested mainsnak → datavalue → value → id structure.
    Only extracts wikibase-entityid values with snaktype "value".

    Args:
        claims: Raw Wikidata claims dict
        pid: Property ID (e.g., "P31", "P279")

    Returns:
        List of QID strings in Wikidata's original order
    """
    statements = claims.get(pid, [])
    qids: list[str] = []
    for stmt in statements:
        mainsnak = stmt.get("mainsnak", {})
        if mainsnak.get("snaktype") != "value":
            continue
        datavalue = mainsnak.get("datavalue", {})
        if datavalue.get("type") != "wikibase-entityid":
            continue
        qid = datavalue.get("value", {}).get("id")
        if qid:
            qids.append(qid)
    return qids


class TagLoader:
    """Loads and manages tag configurations for entity classification."""

    CATEGORY_FILES = [
        "people.yaml",
        "science.yaml",
        "organizations.yaml",
        "places.yaml",
        "events_actions.yaml",
        "creative_works_media.yaml",
        "products_artifacts.yaml",
        "knowledge_information_abstracts.yaml",
    ]

    def __init__(self, tags_dir: str | Path | None = None):
        """
        Initialize the tag loader.

        Args:
            tags_dir: Directory containing YAML tag files.
                      Defaults to the 'tags' subdirectory next to this module.
        """
        if tags_dir is None:
            tags_dir = Path(__file__).parent.parent / "tags"
        self.tags_dir = Path(tags_dir)
        self.categories: dict[str, dict[str, Any]] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all category YAML files."""
        for filename in self.CATEGORY_FILES:
            filepath = self.tags_dir / filename
            if filepath.exists():
                category_name = filename.replace(".yaml", "")
                with open(filepath, "r", encoding="utf-8") as f:
                    self.categories[category_name] = yaml.safe_load(f)

    def get_category_names(self) -> list[str]:
        """Return list of all loaded category names."""
        return list(self.categories.keys())

    def get_modules(self, category: str) -> list[str]:
        """Return list of module names for a category."""
        if category not in self.categories:
            return []
        return list(self.categories[category].get("modules", {}).keys())

    def get_gate_qids(self, category: str) -> set[str]:
        """Return set of gate P31 QIDs for a category."""
        if category not in self.categories:
            return set()
        return set(self.categories[category].get("gate_qids", []))

    def get_all_gate_qids(self) -> dict[str, set[str]]:
        """Return mapping of category -> gate QIDs for all categories."""
        return {cat: self.get_gate_qids(cat) for cat in self.categories}

    def classify(self, entity: dict[str, Any]) -> dict[str, Any]:
        """
        Classify an entity into category and modules.

        Args:
            entity: Dict with Wikidata properties. Expected format:
                {
                    "qid": "Q12345",
                    "properties": {
                        "P31": ["Q5", "Q901"],  # instance of values (QIDs)
                        "P21": ["Q6581097"],    # other properties
                        "P345": ["nm0000001"],  # presence indicates value exists
                        ...
                    }
                }

        Returns:
            {
                "qid": "Q12345",
                "category": "people" | None,
                "modules": ["entertainment", "creativity"],
                "core_props": ["P31", "P21", ...],
                "module_value_props": {"entertainment": ["P136", ...], ...}
            }
        """
        qid = entity.get("qid", "")
        props = entity.get("properties", {})

        # Get P31 values as a set for quick lookup
        p31_values = set(props.get("P31", []))

        result = {
            "qid": qid,
            "category": None,
            "modules": [],
            "core_props": [],
            "module_value_props": {},
        }

        # Step 1: Determine category via gate_qids
        # Iterate through P31 values in order (Wikidata lists primary type first)
        # and find the first matching category
        matched_category = None
        p31_list = props.get("P31", [])
        for p31_qid in p31_list:
            for cat_name, cat_config in self.categories.items():
                gate_qids = set(cat_config.get("gate_qids", []))
                if p31_qid in gate_qids:
                    matched_category = cat_name
                    break
            if matched_category:
                break

        if not matched_category:
            return result

        result["category"] = matched_category
        cat_config = self.categories[matched_category]

        # Step 2: Get core props
        core = cat_config.get("core", {})
        if isinstance(core, dict):
            if "props" in core:
                result["core_props"] = core["props"]
            else:
                # Multiple cores (e.g., science has chemistry and biomedical)
                for core_name, core_props in core.items():
                    if isinstance(core_props, list):
                        result["core_props"].extend(core_props)
                # Deduplicate while preserving order
                seen = set()
                unique_props = []
                for p in result["core_props"]:
                    if p not in seen:
                        seen.add(p)
                        unique_props.append(p)
                result["core_props"] = unique_props

        # Step 3: Match modules
        modules = cat_config.get("modules", {})
        for mod_name, mod_config in modules.items():
            if self._matches_module(props, mod_config):
                result["modules"].append(mod_name)
                result["module_value_props"][mod_name] = mod_config.get("value_props", [])

        return result

    def _matches_module(self, props: dict[str, list], mod_config: dict) -> bool:
        """
        Check if entity properties match a module's indicators.

        Indicators can be:
        - Value-based: P31: [Q123, Q456] -> entity must have P31 with one of these values
        - Presence-based: P345: (null) -> entity must have P345 with any value
        """
        indicators = mod_config.get("indicators", {})
        if not indicators:
            return False

        for prop_id, expected_values in indicators.items():
            entity_values = props.get(prop_id, [])

            if expected_values is None:
                # Presence-based: just check if property exists with any value
                if entity_values:
                    return True
            else:
                # Value-based: check if any expected value is in entity values
                if isinstance(expected_values, list):
                    if set(expected_values) & set(entity_values):
                        return True

        return False

    def classify_raw(self, claims: dict[str, list]) -> dict[str, Any]:
        """
        Classify an entity using raw Wikidata claims (with rank, qualifiers, references).

        Implements P31-first gate matching with P279 fallback, and tracks
        which gate QID matched, which property triggered the match, and
        which indicators matched per module.

        Args:
            claims: Raw Wikidata claims dict, e.g. {"P31": [{"mainsnak": {...}, "rank": ...}], ...}

        Returns:
            {
                "category": "places" | None,
                "modules": ["entertainment", ...],
                "gate_matched": "Q79007" | None,
                "gate_property": "P31" | "P279" | None,
                "matched_indicators": {"entertainment": ["P345", "P31:Q11424"], ...} | None,
            }
        """
        p31_values = extract_qid_values(claims, "P31")
        p279_values = extract_qid_values(claims, "P279")

        result: dict[str, Any] = {
            "category": None,
            "modules": [],
            "gate_matched": None,
            "gate_property": None,
            "matched_indicators": None,
        }

        # Step 1: Gate matching — P31-first, P279 fallback
        matched_category = None
        for p31_qid in p31_values:
            for cat_name, cat_config in self.categories.items():
                gate_qids = set(cat_config.get("gate_qids", []))
                if p31_qid in gate_qids:
                    matched_category = cat_name
                    result["gate_matched"] = p31_qid
                    result["gate_property"] = "P31"
                    break
            if matched_category:
                break

        if not matched_category:
            for p279_qid in p279_values:
                for cat_name, cat_config in self.categories.items():
                    gate_qids = set(cat_config.get("gate_qids", []))
                    if p279_qid in gate_qids:
                        matched_category = cat_name
                        result["gate_matched"] = p279_qid
                        result["gate_property"] = "P279"
                        break
                if matched_category:
                    break

        if not matched_category:
            return result

        result["category"] = matched_category
        cat_config = self.categories[matched_category]

        # Step 2: Module matching with indicator tracking
        # Build a flat property set: PIDs present + P31/P279 QID values for value-based matching
        entity_pids = set(claims.keys())
        p31_set = set(p31_values)

        matched_indicators: dict[str, list[str]] = {}
        modules = cat_config.get("modules", {})
        for mod_name in sorted(modules.keys()):
            mod_config = modules[mod_name]
            indicators = mod_config.get("indicators", {})
            if not indicators:
                continue
            mod_matches: list[str] = []
            for prop_id in sorted(indicators.keys()):
                expected_values = indicators[prop_id]
                if expected_values is None:
                    # Presence-based
                    if prop_id in entity_pids:
                        mod_matches.append(prop_id)
                elif isinstance(expected_values, list):
                    # Value-based (P31 indicator values)
                    if prop_id == "P31":
                        hits = sorted(set(expected_values) & p31_set)
                        for h in hits:
                            mod_matches.append(f"P31:{h}")
                    else:
                        entity_vals = set(extract_qid_values(claims, prop_id))
                        hits = sorted(set(expected_values) & entity_vals)
                        for h in hits:
                            mod_matches.append(f"{prop_id}:{h}")
            if mod_matches:
                result["modules"].append(mod_name)
                matched_indicators[mod_name] = mod_matches

        if matched_indicators:
            result["matched_indicators"] = matched_indicators

        return result

    def classify_by_p31(self, p31_values: list[str]) -> dict[str, Any]:
        """
        Convenience method to classify by P31 values only.

        Args:
            p31_values: List of P31 QID values (e.g., ["Q5", "Q901"])

        Returns:
            Classification result dict
        """
        return self.classify({
            "qid": "",
            "properties": {"P31": p31_values}
        })

    def get_value_props_for_entity(self, entity: dict[str, Any]) -> list[str]:
        """
        Get all relevant value properties for an entity.

        Combines core props with value_props from matched modules.
        """
        result = self.classify(entity)
        all_props = list(result["core_props"])

        for mod_props in result["module_value_props"].values():
            for p in mod_props:
                if p not in all_props:
                    all_props.append(p)

        return all_props


# Singleton instance for convenience
_loader: TagLoader | None = None


def get_loader() -> TagLoader:
    """Get or create the singleton TagLoader instance."""
    global _loader
    if _loader is None:
        _loader = TagLoader()
    return _loader


def classify(entity: dict[str, Any]) -> dict[str, Any]:
    """Convenience function to classify an entity using the singleton loader."""
    return get_loader().classify(entity)


def classify_by_p31(p31_values: list[str]) -> dict[str, Any]:
    """Convenience function to classify by P31 values only."""
    return get_loader().classify_by_p31(p31_values)
