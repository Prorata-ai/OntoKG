use std::collections::{HashMap, HashSet};
use std::fmt;
use std::path::PathBuf;

use crate::error::TagError;
use crate::loader::TagLoader;
use crate::model::CategoryConfig;
use crate::util::{is_valid_pid, is_valid_qid};

#[derive(Debug, Clone)]
pub struct ValidationError {
    pub category: String,
    pub level: String,
    pub location: String,
    pub message: String,
}

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "[{}] {}/{}: {}",
            self.level, self.category, self.location, self.message
        )
    }
}

#[derive(Debug, Clone, Default)]
pub struct ValidationResult {
    pub errors: Vec<ValidationError>,
    pub warnings: Vec<ValidationError>,
    pub stats: HashMap<String, usize>,
}

impl ValidationResult {
    pub fn is_valid(&self) -> bool {
        self.errors.is_empty()
    }

    pub fn add_error(&mut self, category: &str, location: &str, message: &str) {
        self.errors.push(ValidationError {
            category: category.to_string(),
            level: "error".to_string(),
            location: location.to_string(),
            message: message.to_string(),
        });
    }

    pub fn add_warning(&mut self, category: &str, location: &str, message: &str) {
        self.warnings.push(ValidationError {
            category: category.to_string(),
            level: "warning".to_string(),
            location: location.to_string(),
            message: message.to_string(),
        });
    }

    pub fn summary(&self) -> String {
        let status = if self.is_valid() { "PASSED" } else { "FAILED" };
        let mut lines = vec![
            format!("Validation {}", status),
            format!("  Errors: {}", self.errors.len()),
            format!("  Warnings: {}", self.warnings.len()),
        ];
        if !self.stats.is_empty() {
            lines.push("  Stats:".to_string());
            let mut keys: Vec<&String> = self.stats.keys().collect();
            keys.sort();
            for k in keys {
                lines.push(format!("    {}: {}", k, self.stats[k]));
            }
        }
        lines.join("\n")
    }
}

const VALID_MODULE_TYPES: &[&str] = &["intrinsic", "relational"];

pub struct TagValidator {
    loader: TagLoader,
}

impl TagValidator {
    pub fn new(tags_dir: Option<PathBuf>) -> Result<Self, TagError> {
        let loader = TagLoader::new(tags_dir)?;
        Ok(TagValidator { loader })
    }

    pub fn validate(
        &self,
        overlap_threshold: f64,
        gate_exclusive: bool,
    ) -> ValidationResult {
        let mut result = ValidationResult::default();

        let mut all_gate_qids: HashMap<String, Vec<String>> = HashMap::new();
        let mut all_qids: HashSet<String> = HashSet::new();
        let mut all_pids: HashSet<String> = HashSet::new();

        for (cat_name, cat_config) in &self.loader.categories {
            // Structure validation
            self.validate_structure(cat_name, cat_config, &mut result);

            // Gate QIDs
            let gate_qids_set: HashSet<String> =
                cat_config.gate_qids.iter().cloned().collect();

            for qid in &cat_config.gate_qids {
                if !is_valid_qid(qid) {
                    result.add_error(
                        cat_name,
                        "gate_qids",
                        &format!("Invalid QID format: {}", qid),
                    );
                } else {
                    all_qids.insert(qid.clone());
                    all_gate_qids
                        .entry(qid.clone())
                        .or_default()
                        .push(cat_name.clone());
                }
            }

            // Core props
            let core_props = self.loader.extract_core_props(cat_config);
            for pid in core_props {
                if !is_valid_pid(&pid) {
                    result.add_error(cat_name, "core", &format!("Invalid PID format: {}", pid));
                } else {
                    all_pids.insert(pid);
                }
            }

            // Modules
            for (mod_name, mod_config) in &cat_config.modules {
                // Validate module type
                if let Some(ref mod_type) = mod_config.module_type {
                    if !VALID_MODULE_TYPES.contains(&mod_type.as_str()) {
                        result.add_error(
                            cat_name,
                            &format!("modules.{}.type", mod_name),
                            &format!(
                                "Invalid type '{}', must be 'intrinsic' or 'relational'",
                                mod_type
                            ),
                        );
                    }
                } else {
                    result.add_warning(
                        cat_name,
                        &format!("modules.{}", mod_name),
                        "Missing key: type",
                    );
                }

                // Check for P31-only indicator with no QIDs
                if mod_config.indicators.len() == 1 && mod_config.indicators.contains_key("P31") {
                    if let Some(Some(values)) = mod_config.indicators.get("P31") {
                        if values.is_empty() {
                            result.add_warning(
                                cat_name,
                                &format!("modules.{}.indicators.P31", mod_name),
                                "P31 indicator has no QIDs and is the only trigger; this matches all entities with P31",
                            );
                        }
                    } else if mod_config.indicators.get("P31") == Some(&None) {
                        result.add_warning(
                            cat_name,
                            &format!("modules.{}.indicators.P31", mod_name),
                            "P31 indicator has no QIDs and is the only trigger; this matches all entities with P31",
                        );
                    }
                }

                for (pid, values) in &mod_config.indicators {
                    if !is_valid_pid(pid) {
                        result.add_error(
                            cat_name,
                            &format!("modules.{}.indicators", mod_name),
                            &format!("Invalid PID format: {}", pid),
                        );
                    } else {
                        all_pids.insert(pid.clone());
                    }

                    if let Some(values) = values {
                        for (i, qid) in values.iter().enumerate() {
                            if !is_valid_qid(qid) {
                                result.add_error(
                                    cat_name,
                                    &format!("modules.{}.indicators.{}", mod_name, pid),
                                    &format!("Invalid QID format: {}", qid),
                                );
                            } else {
                                all_qids.insert(qid.clone());
                            }

                            // P31 indicator QIDs must be in gate_qids
                            if pid == "P31" && is_valid_qid(qid) && !gate_qids_set.contains(qid) {
                                result.add_error(
                                    cat_name,
                                    &format!(
                                        "modules.{}.indicators.P31[{}]",
                                        mod_name, i
                                    ),
                                    &format!(
                                        "P31 indicator QID {} is not in gate_qids for category",
                                        qid
                                    ),
                                );
                            }
                        }
                    }
                }

                for pid in &mod_config.value_props {
                    if !is_valid_pid(pid) {
                        result.add_error(
                            cat_name,
                            &format!("modules.{}.value_props", mod_name),
                            &format!("Invalid PID format: {}", pid),
                        );
                    } else {
                        all_pids.insert(pid.clone());
                    }
                }
            }

            // P106 overlap within category
            self.validate_p106_overlap(cat_name, cat_config, &mut result);
        }

        // Cross-category: duplicate gate QIDs
        for (qid, categories) in &all_gate_qids {
            if categories.len() > 1 {
                let mut sorted = categories.clone();
                sorted.sort();
                if gate_exclusive {
                    result.add_error(
                        "cross-category",
                        &format!("gate_qids.{}", qid),
                        &format!("QID {} appears in multiple categories: {:?}", qid, sorted),
                    );
                } else {
                    result.add_warning(
                        "cross-category",
                        &format!("gate_qids.{}", qid),
                        &format!("QID {} appears in multiple categories: {:?}", qid, sorted),
                    );
                }
            }
        }

        // Intra-category module overlap
        for (cat_name, cat_config) in &self.loader.categories {
            self.validate_intra_category_overlap(cat_name, cat_config, &mut result, overlap_threshold);
        }

        // Cross-category module overlap
        self.validate_cross_category_overlap(&mut result, overlap_threshold);

        // Stats
        result.stats.insert("categories".to_string(), self.loader.categories.len());
        let total_modules: usize = self
            .loader
            .categories
            .values()
            .map(|config| config.modules.len())
            .sum();
        result.stats.insert("total_modules".to_string(), total_modules);
        result.stats.insert("unique_gate_qids".to_string(), all_gate_qids.len());
        result.stats.insert("unique_qids".to_string(), all_qids.len());
        result.stats.insert("unique_pids".to_string(), all_pids.len());

        result
    }

    fn validate_structure(
        &self,
        cat_name: &str,
        config: &CategoryConfig,
        result: &mut ValidationResult,
    ) {
        if config.gate_qids.is_empty() {
            result.add_error(cat_name, "root", "Missing gate_qids");
        }

        if config.modules.is_empty() {
            result.add_warning(cat_name, "root", "No modules defined");
        }
    }

    fn validate_p106_overlap(
        &self,
        cat_name: &str,
        config: &CategoryConfig,
        result: &mut ValidationResult,
    ) {
        let mut p106_to_modules: HashMap<String, Vec<String>> = HashMap::new();

        for (mod_name, mod_config) in &config.modules {
            if let Some(Some(values)) = mod_config.indicators.get("P106") {
                for qid in values {
                    if is_valid_qid(qid) {
                        p106_to_modules
                            .entry(qid.clone())
                            .or_default()
                            .push(mod_name.clone());
                    }
                }
            }
        }

        for (qid, mod_names) in &p106_to_modules {
            if mod_names.len() > 1 {
                let mut sorted = mod_names.clone();
                sorted.sort();
                result.add_error(
                    cat_name,
                    &format!("modules.P106.{}", qid),
                    &format!("P106 QID {} appears in multiple modules: {:?}", qid, sorted),
                );
            }
        }
    }

    fn validate_intra_category_overlap(
        &self,
        cat_name: &str,
        config: &CategoryConfig,
        result: &mut ValidationResult,
        threshold: f64,
    ) {
        let mut module_qids: HashMap<String, HashSet<String>> = HashMap::new();

        for (mod_name, mod_config) in &config.modules {
            let mut qids = HashSet::new();
            for values in mod_config.indicators.values() {
                if let Some(values) = values {
                    for qid in values {
                        qids.insert(qid.clone());
                    }
                }
            }
            if !qids.is_empty() {
                module_qids.insert(mod_name.clone(), qids);
            }
        }

        let mod_names: Vec<String> = module_qids.keys().cloned().collect();
        for i in 0..mod_names.len() {
            for j in (i + 1)..mod_names.len() {
                let mod_a = &mod_names[i];
                let mod_b = &mod_names[j];

                let qids_a = &module_qids[mod_a];
                let qids_b = &module_qids[mod_b];

                let intersection_count = qids_a.intersection(qids_b).count();
                if intersection_count == 0 {
                    continue;
                }

                let union_count = qids_a.union(qids_b).count();
                let jaccard = intersection_count as f64 / union_count as f64;

                if jaccard >= threshold {
                    let preview: Vec<String> =
                        qids_a.intersection(qids_b).take(5).cloned().collect();
                    let ellipsis = if intersection_count > 5 { "..." } else { "" };
                    result.add_warning(
                        cat_name,
                        &format!("modules.{}+{}", mod_a, mod_b),
                        &format!(
                            "{}% Jaccard ({}/{} QIDs): {:?}{}",
                            (jaccard * 100.0) as i32,
                            intersection_count,
                            union_count,
                            preview,
                            ellipsis
                        ),
                    );
                }
            }
        }
    }

    fn validate_cross_category_overlap(
        &self,
        result: &mut ValidationResult,
        threshold: f64,
    ) {
        let mut all_module_qids: HashMap<String, HashSet<String>> = HashMap::new();

        for (cat_name, cat_config) in &self.loader.categories {
            for (mod_name, mod_config) in &cat_config.modules {
                let key = format!("{}.{}", cat_name, mod_name);
                let mut qids = HashSet::new();

                for values in mod_config.indicators.values() {
                    if let Some(values) = values {
                        for qid in values {
                            qids.insert(qid.clone());
                        }
                    }
                }

                if !qids.is_empty() {
                    all_module_qids.insert(key, qids);
                }
            }
        }

        let keys: Vec<String> = all_module_qids.keys().cloned().collect();
        for i in 0..keys.len() {
            for j in (i + 1)..keys.len() {
                let key_a = &keys[i];
                let key_b = &keys[j];

                // Skip same category
                let cat_a = key_a.split('.').next().unwrap_or("");
                let cat_b = key_b.split('.').next().unwrap_or("");
                if cat_a == cat_b {
                    continue;
                }

                let qids_a = &all_module_qids[key_a];
                let qids_b = &all_module_qids[key_b];

                let intersection_count = qids_a.intersection(qids_b).count();
                if intersection_count == 0 {
                    continue;
                }

                let union_count = qids_a.union(qids_b).count();
                let jaccard = intersection_count as f64 / union_count as f64;

                if jaccard >= threshold {
                    let preview: Vec<String> =
                        qids_a.intersection(qids_b).take(5).cloned().collect();
                    let ellipsis = if intersection_count > 5 { "..." } else { "" };
                    result.add_warning(
                        "cross-category",
                        &format!("modules.{}+{}", key_a, key_b),
                        &format!(
                            "{}% Jaccard ({}/{} QIDs): {:?}{}",
                            (jaccard * 100.0) as i32,
                            intersection_count,
                            union_count,
                            preview,
                            ellipsis
                        ),
                    );
                }
            }
        }
    }
}
