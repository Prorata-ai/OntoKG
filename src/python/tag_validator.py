#!/usr/bin/env python3
"""
Schema Validator for Wikidata Tag Configuration Files

Validates YAML tag files for:
- Structure correctness
- QID/PID format
- Cross-category consistency
- Optional: Wikidata API existence checks
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import lmdb
    HAS_LMDB = True
except ImportError:
    HAS_LMDB = False


@dataclass
class ValidationError:
    """Represents a validation error."""
    category: str
    level: str  # "error" or "warning"
    location: str  # e.g., "modules.finance.indicators.P31"
    message: str

    def __str__(self) -> str:
        return f"[{self.level.upper()}] {self.category}/{self.location}: {self.message}"


@dataclass
class ValidationResult:
    """Results of validation."""
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, category: str, location: str, message: str) -> None:
        self.errors.append(ValidationError(category, "error", location, message))

    def add_warning(self, category: str, location: str, message: str) -> None:
        self.warnings.append(ValidationError(category, "warning", location, message))

    def summary(self) -> str:
        lines = [
            f"Validation {'PASSED' if self.is_valid else 'FAILED'}",
            f"  Errors: {len(self.errors)}",
            f"  Warnings: {len(self.warnings)}",
        ]
        if self.stats:
            lines.append("  Stats:")
            for k, v in self.stats.items():
                lines.append(f"    {k}: {v}")
        return "\n".join(lines)


# --- Comment verification helpers ---

@dataclass(frozen=True)
class CommentOccurrence:
    """A QID/PID occurrence with its inline comment."""
    path: Path
    line_no: int
    qid_or_pid: str
    comment: str | None


COMMENT_ID_PATTERNS = [
    re.compile(r"^\s*-\s*(?P<id>[QP]\d+)\b"),
    re.compile(r"^\s*(?P<id>[QP]\d+)\s*:"),
]


def _normalize_label(text: str) -> str:
    """Normalize text for comparison."""
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _comment_matches_label(comment: str, label: str, strict: bool) -> bool:
    """Check if comment matches the label."""
    comment_norm = _normalize_label(comment)
    label_norm = _normalize_label(label)
    if strict:
        return comment_norm == label_norm
    if comment_norm == label_norm:
        return True
    if comment_norm.startswith(label_norm) or label_norm.startswith(comment_norm):
        return True
    if re.search(rf"\b{re.escape(label_norm)}\b", comment_norm):
        return True
    if re.search(rf"\b{re.escape(comment_norm)}\b", label_norm):
        return True
    return False


def _extract_comment_occurrences(path: Path) -> list[CommentOccurrence]:
    """Extract QID/PID occurrences with their inline comments from a YAML file."""
    occurrences: list[CommentOccurrence] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue

            if "#" in line:
                body, comment = line.split("#", 1)
                comment = comment.strip() or None
            else:
                body = line
                comment = None

            qid_or_pid = None
            for pattern in COMMENT_ID_PATTERNS:
                match = pattern.match(body)
                if match:
                    qid_or_pid = match.group("id")
                    break

            if qid_or_pid is None:
                continue

            occurrences.append(
                CommentOccurrence(path=path, line_no=line_no, qid_or_pid=qid_or_pid, comment=comment)
            )
    return occurrences


class TagValidator:
    """Validates tag configuration YAML files."""

    QID_PATTERN = re.compile(r"^Q\d+$")
    PID_PATTERN = re.compile(r"^P\d+$")

    REQUIRED_TOP_KEYS = ["gate_qids", "modules"]
    REQUIRED_MODULE_KEYS = ["type", "indicators", "value_props"]
    VALID_MODULE_TYPES = {"intrinsic", "relational"}

    def __init__(self, tags_dir: str | Path | None = None):
        """
        Initialize validator.

        Args:
            tags_dir: Directory containing YAML tag files.
        """
        if tags_dir is None:
            tags_dir = Path(__file__).parent.parent / "tags"
        self.tags_dir = Path(tags_dir)
        self.categories: dict[str, dict[str, Any]] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all YAML files from tags directory."""
        for filepath in self.tags_dir.glob("*.yaml"):
            category_name = filepath.stem
            with open(filepath, "r", encoding="utf-8") as f:
                self.categories[category_name] = yaml.safe_load(f)

    def validate(
        self,
        check_wikidata: bool = False,
        overlap_threshold: float = 0.5,
        gate_exclusive: bool = False,
        check_comments: bool = False,
        fix_missing_comments: bool = False,
        delete_mismatch: bool = False,
        delete_missing_label: bool = False,
        lmdb_path: str | Path | None = None,
        strict_comments: bool = True,
        action_log_path: str | Path | None = None,
    ) -> ValidationResult:
        """
        Run all validations.

        Args:
            check_wikidata: If True, validate QIDs/PIDs exist in Wikidata (requires network)
            overlap_threshold: Jaccard similarity (0.0-1.0) to trigger warnings
            gate_exclusive: If True, gate_qids must be exclusive across categories (duplicates become errors)
            check_comments: If True, verify inline comments match LMDB labels
            fix_missing_comments: If True, add missing comments from LMDB labels
            delete_mismatch: If True, delete lines where comment doesn't match LMDB label
            delete_missing_label: If True, delete lines where QID/PID not found in LMDB
            lmdb_path: Path to LMDB labels database (required if check_comments=True)
            strict_comments: If True, require exact match; if False, allow partial matches
            action_log_path: Path to write action log (fill/delete operations)

        Returns:
            ValidationResult with errors, warnings, and stats
        """
        result = ValidationResult()

        # Collect all QIDs and PIDs for stats and cross-validation
        all_gate_qids: dict[str, list[str]] = {}  # qid -> [categories]
        all_qids: set[str] = set()
        all_pids: set[str] = set()

        for cat_name, cat_config in self.categories.items():
            # Structure validation
            self._validate_structure(cat_name, cat_config, result)

            # Format validation and collection
            gate_qids = self._validate_gate_qids(cat_name, cat_config, result)
            for qid in gate_qids:
                all_qids.add(qid)
                if qid not in all_gate_qids:
                    all_gate_qids[qid] = []
                all_gate_qids[qid].append(cat_name)

            # Core props validation
            core_pids = self._validate_core(cat_name, cat_config, result)
            all_pids.update(core_pids)

            # Modules validation
            module_qids, module_pids = self._validate_modules(
                cat_name, cat_config, result, set(gate_qids)
            )
            all_qids.update(module_qids)
            all_pids.update(module_pids)

        # Cross-category validation: check for duplicate gate QIDs
        for qid, categories in all_gate_qids.items():
            if len(categories) > 1:
                if gate_exclusive:
                    result.add_error(
                        "cross-category",
                        f"gate_qids.{qid}",
                        f"QID {qid} appears in multiple categories: {categories}"
                    )
                else:
                    result.add_warning(
                        "cross-category",
                        f"gate_qids.{qid}",
                        f"QID {qid} appears in multiple categories: {categories}"
                    )

        # P106 overlap validation: same P106 QID in multiple modules is an error
        for cat_name, cat_config in self.categories.items():
            self._validate_p106_overlap(cat_name, cat_config, result)

        # Module overlap validation within each category
        for cat_name, cat_config in self.categories.items():
            self._validate_module_overlap(cat_name, cat_config, result, overlap_threshold)

        # Module overlap validation across categories
        self._validate_cross_category_module_overlap(result, overlap_threshold)

        # Stats
        result.stats = {
            "categories": len(self.categories),
            "total_modules": sum(
                len(c.get("modules", {})) for c in self.categories.values()
            ),
            "unique_gate_qids": len(all_gate_qids),
            "unique_qids": len(all_qids),
            "unique_pids": len(all_pids),
        }

        # Optional: Wikidata existence check
        if check_wikidata:
            if not HAS_REQUESTS:
                result.add_error("validator", "config", "requests library required for Wikidata validation")
            else:
                self._validate_wikidata_existence(all_qids, all_pids, result)

        # Optional: Comment verification against LMDB
        if check_comments or fix_missing_comments or delete_mismatch or delete_missing_label:
            if not HAS_LMDB:
                result.add_error("validator", "config", "lmdb library required for comment verification")
            elif lmdb_path is None:
                result.add_error("validator", "config", "lmdb_path required for comment verification")
            else:
                self._validate_comments(
                    Path(lmdb_path), strict_comments, fix_missing_comments,
                    delete_mismatch, delete_missing_label, action_log_path, result
                )

        return result

    def _validate_structure(
        self, cat_name: str, cat_config: dict, result: ValidationResult
    ) -> None:
        """Validate top-level structure."""
        if cat_config is None:
            result.add_error(cat_name, "root", "Empty or invalid YAML file")
            return

        for key in self.REQUIRED_TOP_KEYS:
            if key not in cat_config:
                result.add_error(cat_name, "root", f"Missing required key: {key}")

    def _validate_gate_qids(
        self, cat_name: str, cat_config: dict, result: ValidationResult
    ) -> list[str]:
        """Validate gate_qids format."""
        gate_qids = cat_config.get("gate_qids", [])
        valid_qids = []

        if not isinstance(gate_qids, list):
            result.add_error(cat_name, "gate_qids", "Must be a list")
            return valid_qids

        for i, qid in enumerate(gate_qids):
            if not isinstance(qid, str):
                result.add_error(cat_name, f"gate_qids[{i}]", f"Must be string, got {type(qid).__name__}")
            elif not self.QID_PATTERN.match(qid):
                result.add_error(cat_name, f"gate_qids[{i}]", f"Invalid QID format: {qid}")
            else:
                valid_qids.append(qid)

        return valid_qids

    def _validate_core(
        self, cat_name: str, cat_config: dict, result: ValidationResult
    ) -> set[str]:
        """Validate core props."""
        pids = set()
        core = cat_config.get("core")

        if core is None:
            result.add_warning(cat_name, "core", "No core section defined")
            return pids

        if not isinstance(core, dict):
            result.add_error(cat_name, "core", "Must be a dict")
            return pids

        # Handle single core with "props" or multiple named cores
        if "props" in core:
            pids.update(self._validate_prop_list(cat_name, "core.props", core["props"], result))
        else:
            # Multiple cores (e.g., science has chemistry and biomedical)
            for core_name, props in core.items():
                if isinstance(props, list):
                    pids.update(self._validate_prop_list(
                        cat_name, f"core.{core_name}", props, result
                    ))

        return pids

    def _validate_prop_list(
        self, cat_name: str, location: str, props: list, result: ValidationResult
    ) -> set[str]:
        """Validate a list of property IDs."""
        pids = set()

        if not isinstance(props, list):
            result.add_error(cat_name, location, "Must be a list")
            return pids

        for i, pid in enumerate(props):
            if not isinstance(pid, str):
                result.add_error(cat_name, f"{location}[{i}]", f"Must be string, got {type(pid).__name__}")
            elif not self.PID_PATTERN.match(pid):
                result.add_error(cat_name, f"{location}[{i}]", f"Invalid PID format: {pid}")
            else:
                pids.add(pid)

        return pids

    def _validate_modules(
        self, cat_name: str, cat_config: dict, result: ValidationResult, gate_qids_set: set[str]
    ) -> tuple[set[str], set[str]]:
        """Validate modules section."""
        all_qids = set()
        all_pids = set()

        modules = cat_config.get("modules", {})

        if not isinstance(modules, dict):
            result.add_error(cat_name, "modules", "Must be a dict")
            return all_qids, all_pids

        for mod_name, mod_config in modules.items():
            if mod_config is None:
                result.add_error(cat_name, f"modules.{mod_name}", "Module is empty")
                continue

            if not isinstance(mod_config, dict):
                result.add_error(cat_name, f"modules.{mod_name}", "Must be a dict")
                continue

            # Check required keys
            for key in self.REQUIRED_MODULE_KEYS:
                if key not in mod_config:
                    result.add_warning(cat_name, f"modules.{mod_name}", f"Missing key: {key}")

            # Validate module type
            mod_type = mod_config.get("type")
            if mod_type is not None and mod_type not in self.VALID_MODULE_TYPES:
                result.add_error(
                    cat_name, f"modules.{mod_name}.type",
                    f"Invalid type '{mod_type}', must be 'intrinsic' or 'relational'"
                )

            # Validate indicators
            indicators = mod_config.get("indicators", {})
            if isinstance(indicators, dict):
                if set(indicators.keys()) == {"P31"}:
                    p31_values = indicators.get("P31")
                    if p31_values is None or (isinstance(p31_values, list) and len(p31_values) == 0):
                        result.add_warning(
                            cat_name,
                            f"modules.{mod_name}.indicators.P31",
                            "P31 indicator has no QIDs and is the only trigger; this matches all entities with P31"
                        )
            if indicators:
                qids, pids = self._validate_indicators(
                    cat_name, f"modules.{mod_name}.indicators", indicators, result
                )
                all_qids.update(qids)
                all_pids.update(pids)

                # Enforce: module P31 QIDs must be in gate_qids for this category
                if isinstance(indicators, dict) and "P31" in indicators:
                    p31_values = indicators.get("P31")
                    if isinstance(p31_values, list):
                        for i, qid in enumerate(p31_values):
                            if isinstance(qid, str) and qid.startswith("Q") and qid not in gate_qids_set:
                                result.add_error(
                                    cat_name,
                                    f"modules.{mod_name}.indicators.P31[{i}]",
                                    f"P31 indicator QID {qid} is not in gate_qids for category"
                                )

            # Validate value_props
            value_props = mod_config.get("value_props", [])
            if value_props:
                all_pids.update(self._validate_prop_list(
                    cat_name, f"modules.{mod_name}.value_props", value_props, result
                ))

        return all_qids, all_pids

    def _validate_indicators(
        self, cat_name: str, location: str, indicators: dict, result: ValidationResult
    ) -> tuple[set[str], set[str]]:
        """Validate module indicators."""
        qids = set()
        pids = set()

        if not isinstance(indicators, dict):
            result.add_error(cat_name, location, "Must be a dict")
            return qids, pids

        for pid, values in indicators.items():
            # Validate PID format
            if not self.PID_PATTERN.match(pid):
                result.add_error(cat_name, f"{location}.{pid}", f"Invalid PID format: {pid}")
            else:
                pids.add(pid)

            # Values can be None (presence-based) or list of QIDs (value-based)
            if values is None:
                # Presence-based indicator - valid
                continue
            elif isinstance(values, list):
                for i, qid in enumerate(values):
                    if not isinstance(qid, str):
                        result.add_error(
                            cat_name, f"{location}.{pid}[{i}]",
                            f"Must be string, got {type(qid).__name__}"
                        )
                    elif not self.QID_PATTERN.match(qid):
                        result.add_error(
                            cat_name, f"{location}.{pid}[{i}]",
                            f"Invalid QID format: {qid}"
                        )
                    else:
                        qids.add(qid)
            else:
                result.add_error(
                    cat_name, f"{location}.{pid}",
                    f"Must be null or list, got {type(values).__name__}"
                )

        return qids, pids

    def _validate_p106_overlap(
        self, cat_name: str, cat_config: dict, result: ValidationResult
    ) -> None:
        """
        Check for P106 QIDs that appear in multiple modules within a category.

        Each P106 QID should map to exactly one module — duplicates cause
        non-deterministic classification depending on module iteration order.
        """
        modules = cat_config.get("modules", {})
        if not modules:
            return

        # Collect P106 QIDs per module
        p106_to_modules: dict[str, list[str]] = {}  # qid -> [module names]
        for mod_name, mod_config in modules.items():
            if not mod_config:
                continue
            indicators = mod_config.get("indicators", {})
            if not isinstance(indicators, dict):
                continue
            p106_values = indicators.get("P106")
            if not isinstance(p106_values, list):
                continue
            for qid in p106_values:
                if isinstance(qid, str) and self.QID_PATTERN.match(qid):
                    if qid not in p106_to_modules:
                        p106_to_modules[qid] = []
                    p106_to_modules[qid].append(mod_name)

        # Report duplicates
        for qid, mod_names in sorted(p106_to_modules.items()):
            if len(mod_names) > 1:
                result.add_error(
                    cat_name,
                    f"modules.P106.{qid}",
                    f"P106 QID {qid} appears in multiple modules: {mod_names}"
                )

    def _validate_module_overlap(
        self, cat_name: str, cat_config: dict, result: ValidationResult,
        threshold: float = 0.5
    ) -> None:
        """
        Check for overlapping indicator QIDs between modules.

        Args:
            cat_name: Category name
            cat_config: Category configuration
            result: ValidationResult to add warnings to
            threshold: Jaccard similarity threshold (0.0-1.0) to trigger warning
        """
        modules = cat_config.get("modules", {})
        if not modules:
            return

        # Collect indicator QIDs for each module
        module_qids: dict[str, set[str]] = {}
        for mod_name, mod_config in modules.items():
            if not mod_config:
                continue
            indicators = mod_config.get("indicators", {})
            if not indicators:
                continue

            qids = set()
            for pid, values in indicators.items():
                if isinstance(values, list):
                    qids.update(v for v in values if isinstance(v, str))
            module_qids[mod_name] = qids

        # Compare all pairs of modules
        mod_names = list(module_qids.keys())
        for i, mod_a in enumerate(mod_names):
            for mod_b in mod_names[i + 1:]:
                qids_a = module_qids[mod_a]
                qids_b = module_qids[mod_b]

                if not qids_a or not qids_b:
                    continue

                # Calculate overlap
                intersection = qids_a & qids_b
                if not intersection:
                    continue

                # Jaccard similarity: intersection / union
                union = qids_a | qids_b
                jaccard = len(intersection) / len(union)

                if jaccard >= threshold:
                    jaccard_pct = int(jaccard * 100)
                    result.add_warning(
                        cat_name,
                        f"modules.{mod_a}+{mod_b}",
                        f"{jaccard_pct}% Jaccard ({len(intersection)}/{len(union)} QIDs): {sorted(intersection)[:5]}{'...' if len(intersection) > 5 else ''}"
                    )

    def _validate_cross_category_module_overlap(
        self, result: ValidationResult, threshold: float = 0.5
    ) -> None:
        """
        Check for overlapping indicator QIDs between modules across different categories.

        Args:
            result: ValidationResult to add warnings to
            threshold: Jaccard similarity threshold (0.0-1.0) to trigger warning
        """
        # Collect indicator QIDs for each module across all categories
        # Key: "category.module", Value: set of QIDs
        all_module_qids: dict[str, set[str]] = {}

        for cat_name, cat_config in self.categories.items():
            modules = cat_config.get("modules", {})
            if not modules:
                continue

            for mod_name, mod_config in modules.items():
                if not mod_config:
                    continue
                indicators = mod_config.get("indicators", {})
                if not indicators:
                    continue

                qids = set()
                for pid, values in indicators.items():
                    if isinstance(values, list):
                        qids.update(v for v in values if isinstance(v, str))

                if qids:
                    all_module_qids[f"{cat_name}.{mod_name}"] = qids

        # Compare all pairs of modules from different categories
        mod_keys = list(all_module_qids.keys())
        for i, key_a in enumerate(mod_keys):
            cat_a = key_a.split(".")[0]
            for key_b in mod_keys[i + 1:]:
                cat_b = key_b.split(".")[0]

                # Skip if same category (handled by within-category check)
                if cat_a == cat_b:
                    continue

                qids_a = all_module_qids[key_a]
                qids_b = all_module_qids[key_b]

                # Calculate overlap
                intersection = qids_a & qids_b
                if not intersection:
                    continue

                # Jaccard similarity: intersection / union
                union = qids_a | qids_b
                jaccard = len(intersection) / len(union)

                if jaccard >= threshold:
                    jaccard_pct = int(jaccard * 100)
                    result.add_warning(
                        "cross-category",
                        f"modules.{key_a}+{key_b}",
                        f"{jaccard_pct}% Jaccard ({len(intersection)}/{len(union)} QIDs): {sorted(intersection)[:5]}{'...' if len(intersection) > 5 else ''}"
                    )

    def _validate_wikidata_existence(
        self, qids: set[str], pids: set[str], result: ValidationResult
    ) -> None:
        """Check if QIDs and PIDs exist in Wikidata."""
        print("Checking Wikidata existence (this may take a moment)...")

        # Check QIDs in batches
        invalid_qids = self._check_entities_exist(list(qids), "Q")
        for qid in invalid_qids:
            result.add_error("wikidata", f"qid.{qid}", f"QID {qid} does not exist in Wikidata")

        # Check PIDs in batches
        invalid_pids = self._check_entities_exist(list(pids), "P")
        for pid in invalid_pids:
            result.add_error("wikidata", f"pid.{pid}", f"PID {pid} does not exist in Wikidata")

        result.stats["wikidata_invalid_qids"] = len(invalid_qids)
        result.stats["wikidata_invalid_pids"] = len(invalid_pids)

    def _check_entities_exist(self, ids: list[str], prefix: str) -> list[str]:
        """Check if entities exist in Wikidata using the API."""
        invalid = []
        batch_size = 50

        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            ids_param = "|".join(batch)

            try:
                response = requests.get(
                    "https://www.wikidata.org/w/api.php",
                    params={
                        "action": "wbgetentities",
                        "ids": ids_param,
                        "props": "info",
                        "format": "json",
                    },
                    timeout=30,
                )
                data = response.json()

                entities = data.get("entities", {})
                for entity_id in batch:
                    entity = entities.get(entity_id, {})
                    if "missing" in entity:
                        invalid.append(entity_id)

            except Exception as e:
                print(f"Warning: API request failed for batch: {e}")

            # Rate limiting
            time.sleep(0.1)

        return invalid

    def _validate_comments(
        self, lmdb_path: Path, strict: bool, fix: bool, delete_mismatch: bool,
        delete_missing_label: bool, action_log_path: str | Path | None, result: ValidationResult
    ) -> None:
        """Verify inline comments match LMDB labels, optionally fixing missing ones."""
        print("Verifying inline comments against LMDB labels...")

        # Extract all occurrences from YAML files
        occurrences: list[CommentOccurrence] = []
        for filepath in self.tags_dir.glob("*.yaml"):
            occurrences.extend(_extract_comment_occurrences(filepath))

        if not occurrences:
            result.add_warning("comments", "files", "No QID/PID occurrences found in tag files")
            return

        # Load labels from LMDB
        ids = {occ.qid_or_pid for occ in occurrences}
        try:
            env = lmdb.open(str(lmdb_path), readonly=True, lock=False, max_dbs=1)
            dbi = env.open_db(b"labels_en")
            labels: dict[str, str | None] = {}
            with env.begin() as txn:
                for item_id in ids:
                    val = txn.get(item_id.encode("utf-8"), db=dbi)
                    labels[item_id] = val.decode("utf-8") if val else None
            env.close()
        except Exception as e:
            result.add_error("comments", "lmdb", f"Failed to read LMDB: {e}")
            return

        # Check each occurrence and collect fixes/deletions
        missing_comment = 0
        missing_label = 0
        mismatched = 0
        ok_count = 0
        fixed_count = 0
        deleted_count = 0

        # Group by file: fixes (add comment) and deletions (remove line)
        fixes_by_file: dict[Path, list[tuple[int, str]]] = {}  # file -> [(line_no, label)]
        deletions_by_file: dict[Path, set[int]] = {}  # file -> {line_no, ...}

        # Action log entries
        action_log: list[str] = []

        for occ in occurrences:
            label = labels.get(occ.qid_or_pid)
            rel_path = occ.path.name
            location = f"{rel_path}:{occ.line_no}"

            if not occ.comment:
                if label is not None and fix:
                    # Collect fix for this file
                    if occ.path not in fixes_by_file:
                        fixes_by_file[occ.path] = []
                    fixes_by_file[occ.path].append((occ.line_no, label))
                    fixed_count += 1
                    action_log.append(f"FILL\t{location}\t{occ.qid_or_pid}\t{label}")
                else:
                    result.add_warning("comments", location, f"{occ.qid_or_pid} missing inline comment")
                    missing_comment += 1
            elif label is None:
                if delete_missing_label:
                    if occ.path not in deletions_by_file:
                        deletions_by_file[occ.path] = set()
                    deletions_by_file[occ.path].add(occ.line_no)
                    deleted_count += 1
                    print(f"  Deleting {location}: {occ.qid_or_pid} not found in LMDB")
                    action_log.append(f"DELETE_MISSING_LABEL\t{location}\t{occ.qid_or_pid}\t{occ.comment}")
                else:
                    result.add_warning("comments", location, f"{occ.qid_or_pid} not found in LMDB")
                    missing_label += 1
            elif not _comment_matches_label(occ.comment, label, strict):
                if delete_mismatch:
                    # Collect deletion for this file
                    if occ.path not in deletions_by_file:
                        deletions_by_file[occ.path] = set()
                    deletions_by_file[occ.path].add(occ.line_no)
                    deleted_count += 1
                    print(f"  Deleting {location}: '{occ.comment}' != '{label}'")
                    action_log.append(f"DELETE_MISMATCH\t{location}\t{occ.qid_or_pid}\t{occ.comment}\t{label}")
                else:
                    result.add_error(
                        "comments", location,
                        f"{occ.qid_or_pid} comment mismatch: '{occ.comment}' != '{label}'"
                    )
                    mismatched += 1
            else:
                ok_count += 1

        # Apply fixes (add comments)
        if fix and fixes_by_file:
            for filepath, fixes in fixes_by_file.items():
                self._apply_comment_fixes(filepath, fixes)
            print(f"Fixed {fixed_count} missing comments in {len(fixes_by_file)} file(s)")

        # Apply deletions
        if (delete_mismatch or delete_missing_label) and deletions_by_file:
            for filepath, line_numbers in deletions_by_file.items():
                self._delete_lines(filepath, line_numbers)
            print(f"Deleted {deleted_count} lines in {len(deletions_by_file)} file(s)")

        # Write action log
        if action_log_path and action_log:
            log_path = Path(action_log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as f:
                f.write("ACTION\tLOCATION\tID\tCOMMENT\tLABEL\n")
                for entry in action_log:
                    f.write(entry + "\n")
            print(f"Action log written to {log_path}")

        result.stats["comment_entries"] = len(occurrences)
        result.stats["comment_ok"] = ok_count
        result.stats["comment_missing"] = missing_comment
        result.stats["comment_no_label"] = missing_label
        result.stats["comment_mismatch"] = mismatched
        if fix:
            result.stats["comment_fixed"] = fixed_count
        if delete_mismatch or delete_missing_label:
            result.stats["comment_deleted"] = deleted_count

    def _apply_comment_fixes(
        self, filepath: Path, fixes: list[tuple[int, str]]
    ) -> None:
        """Apply comment fixes to a file."""
        # Create a dict for quick lookup: line_no -> label
        fix_map = {line_no: label for line_no, label in fixes}

        # Read all lines
        with filepath.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        # Apply fixes
        for line_no, label in fix_map.items():
            idx = line_no - 1  # Convert to 0-indexed
            if idx < len(lines):
                line = lines[idx].rstrip("\n\r")
                lines[idx] = f"{line}  # {label}\n"

        # Write back
        with filepath.open("w", encoding="utf-8") as f:
            f.writelines(lines)

    def _delete_lines(self, filepath: Path, line_numbers: set[int]) -> None:
        """Delete specified lines from a file."""
        with filepath.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        # Keep lines not in deletion set (1-indexed)
        new_lines = [
            line for i, line in enumerate(lines, start=1)
            if i not in line_numbers
        ]

        with filepath.open("w", encoding="utf-8") as f:
            f.writelines(new_lines)


def validate(check_wikidata: bool = False, gate_exclusive: bool = False) -> ValidationResult:
    """Convenience function to validate all tag files."""
    validator = TagValidator()
    return validator.validate(check_wikidata=check_wikidata, gate_exclusive=gate_exclusive)


def main():
    """Run validation from command line."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate tag configuration files")
    parser.add_argument(
        "--check-wikidata", "-w",
        action="store_true",
        help="Check if QIDs/PIDs exist in Wikidata (requires network)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show all errors and warnings"
    )
    parser.add_argument(
        "--overlap-threshold", "-o",
        type=float,
        default=0.5,
        help="Module Jaccard similarity threshold (0.0-1.0) to trigger warnings (default: 0.5)"
    )
    parser.add_argument(
        "--gate-exclusive",
        action="store_true",
        help="Require gate_qids to be exclusive across categories (duplicates become errors)"
    )
    parser.add_argument(
        "--check-comments", "-c",
        action="store_true",
        help="Verify inline comments match LMDB labels"
    )
    parser.add_argument(
        "--fix-missing-comments", "-f",
        action="store_true",
        help="Add missing comments from LMDB labels"
    )
    parser.add_argument(
        "--delete-mismatch",
        action="store_true",
        help="Delete lines where comment doesn't match LMDB label"
    )
    parser.add_argument(
        "--delete-missing-label",
        action="store_true",
        help="Delete lines where QID/PID not found in LMDB"
    )
    parser.add_argument(
        "--lmdb-path",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "data_dump_output" / "labels_en.lmdb",
        help="Path to LMDB labels database"
    )
    parser.add_argument(
        "--loose-comments",
        action="store_true",
        help="Allow partial comment/label matches (default: strict)"
    )
    parser.add_argument(
        "--action-log",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "logs" / "tag_actions.tsv",
        help="Path to write action log (TSV format)"
    )
    args = parser.parse_args()

    validator = TagValidator()
    result = validator.validate(
        check_wikidata=args.check_wikidata,
        overlap_threshold=args.overlap_threshold,
        gate_exclusive=args.gate_exclusive,
        check_comments=args.check_comments,
        fix_missing_comments=args.fix_missing_comments,
        delete_mismatch=args.delete_mismatch,
        delete_missing_label=args.delete_missing_label,
        lmdb_path=args.lmdb_path,
        strict_comments=not args.loose_comments,
        action_log_path=args.action_log,
    )

    print(result.summary())
    print()

    if args.verbose or not result.is_valid:
        if result.errors:
            print("Errors:")
            for err in result.errors:
                print(f"  {err}")
            print()

        if result.warnings:
            print("Warnings:")
            for warn in result.warnings:
                print(f"  {warn}")


if __name__ == "__main__":
    main()
