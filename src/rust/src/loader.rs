use std::collections::{HashMap, HashSet};
use std::fs;
use std::io;
use std::path::PathBuf;

use serde_yaml::Value;

use crate::error::TagError;
use crate::model::{CategoryConfig, ClassificationResult, Entity, Module};

pub const CATEGORY_FILES: [&str; 8] = [
    "people.yaml",
    "science.yaml",
    "organizations.yaml",
    "places.yaml",
    "events_actions.yaml",
    "creative_works_media.yaml",
    "products_artifacts.yaml",
    "knowledge_information_abstracts.yaml",
];

pub struct TagLoader {
    pub tags_dir: PathBuf,
    pub categories: HashMap<String, CategoryConfig>,
}

impl TagLoader {
    pub fn new(tags_dir: Option<PathBuf>) -> Result<Self, TagError> {
        let tags_dir = tags_dir.unwrap_or_else(|| PathBuf::from("..").join("tags"));
        let mut loader = TagLoader {
            tags_dir,
            categories: HashMap::new(),
        };
        loader.load_all()?;
        Ok(loader)
    }

    fn load_all(&mut self) -> Result<(), TagError> {
        for filename in CATEGORY_FILES {
            let path = self.tags_dir.join(filename);
            let data = match fs::read(&path) {
                Ok(contents) => contents,
                Err(err) => {
                    if err.kind() == io::ErrorKind::NotFound {
                        continue;
                    }
                    return Err(err.into());
                }
            };

            let config: CategoryConfig = serde_yaml::from_slice(&data)?;
            let category_name = filename.trim_end_matches(".yaml");
            self.categories.insert(category_name.to_string(), config);
        }

        Ok(())
    }

    pub fn get_category_names(&self) -> Vec<String> {
        CATEGORY_FILES
            .iter()
            .filter_map(|name| {
                let category = name.trim_end_matches(".yaml");
                if self.categories.contains_key(category) {
                    Some(category.to_string())
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn get_modules(&self, category: &str) -> Vec<String> {
        self.categories
            .get(category)
            .map(|config| {
                let mut names: Vec<String> = config.modules.keys().cloned().collect();
                names.sort();
                names
            })
            .unwrap_or_default()
    }

    pub fn get_gate_qids(&self, category: &str) -> HashSet<String> {
        self.categories
            .get(category)
            .map(|config| config.gate_qids.iter().cloned().collect())
            .unwrap_or_default()
    }

    pub fn classify(&self, entity: &Entity) -> ClassificationResult {
        let mut result = ClassificationResult {
            qid: entity.qid.clone(),
            ..ClassificationResult::default()
        };

        // Try P31 first, then fall back to P279
        let mut matched_category = None;
        let mut gate_property = "";

        // Step 1: Try P31 (instance of)
        if let Some(p31_values) = entity.properties.get("P31") {
            for p31_qid in p31_values {
                for cat_name in CATEGORY_FILES {
                    let category = cat_name.trim_end_matches(".yaml");
                    let Some(cat_config) = self.categories.get(category) else {
                        continue;
                    };
                    for gate_qid in &cat_config.gate_qids {
                        if p31_qid == gate_qid {
                            matched_category = Some(category.to_string());
                            break;
                        }
                    }
                    if matched_category.is_some() {
                        break;
                    }
                }
                if matched_category.is_some() {
                    gate_property = "P31";
                    break;
                }
            }
        }

        // Step 2: Fall back to P279 (subclass of)
        if matched_category.is_none() {
            if let Some(p279_values) = entity.properties.get("P279") {
                for p279_qid in p279_values {
                    for cat_name in CATEGORY_FILES {
                        let category = cat_name.trim_end_matches(".yaml");
                        let Some(cat_config) = self.categories.get(category) else {
                            continue;
                        };
                        for gate_qid in &cat_config.gate_qids {
                            if p279_qid == gate_qid {
                                matched_category = Some(category.to_string());
                                break;
                            }
                        }
                        if matched_category.is_some() {
                            break;
                        }
                    }
                    if matched_category.is_some() {
                        gate_property = "P279";
                        break;
                    }
                }
            }
        }

        let matched_category = match matched_category {
            Some(category) => category,
            None => return result,
        };

        result.category = matched_category.clone();
        result.gate_property = gate_property.to_string();

        if let Some(cat_config) = self.categories.get(&matched_category) {
            result.core_props = self.extract_core_props(cat_config);

            let mut module_names: Vec<String> = cat_config.modules.keys().cloned().collect();
            module_names.sort();
            for mod_name in module_names {
                let Some(mod_config) = cat_config.modules.get(&mod_name) else {
                    continue;
                };
                if self.matches_module(&entity.properties, mod_config) {
                    result.modules.push(mod_name.clone());
                    result
                        .module_value_props
                        .insert(mod_name.clone(), mod_config.value_props.clone());
                }
            }
        }

        result
    }

    pub fn classify_by_p31(&self, p31_values: &[String]) -> ClassificationResult {
        let entity = Entity {
            qid: String::new(),
            properties: HashMap::from([("P31".to_string(), p31_values.to_vec())]),
        };
        self.classify(&entity)
    }

    pub(crate) fn extract_core_props(&self, config: &CategoryConfig) -> Vec<String> {
        let mut props = Vec::new();

        let core = match &config.core {
            Some(value) => value,
            None => return props,
        };

        let mapping = match core.as_mapping() {
            Some(map) => map,
            None => return props,
        };

        if let Some(props_value) = mapping.get(&Value::String("props".to_string())) {
            if let Some(seq) = props_value.as_sequence() {
                for value in seq {
                    if let Some(prop) = value.as_str() {
                        props.push(prop.to_string());
                    }
                }
            }
            return props;
        }

        let mut seen = HashSet::new();
        for value in mapping.values() {
            if let Some(seq) = value.as_sequence() {
                for item in seq {
                    if let Some(prop) = item.as_str() {
                        let prop = prop.to_string();
                        if seen.insert(prop.clone()) {
                            props.push(prop);
                        }
                    }
                }
            }
        }

        props
    }

    fn matches_module(&self, props: &HashMap<String, Vec<String>>, module: &Module) -> bool {
        if module.indicators.is_empty() {
            return false;
        }

        for (prop_id, expected_values) in &module.indicators {
            let entity_values = props.get(prop_id).map(Vec::as_slice).unwrap_or(&[]);

            match expected_values {
                None => {
                    // Presence-based indicator: property key exists, even when values are unknown.
                    if props.contains_key(prop_id) {
                        return true;
                    }
                }
                Some(values) => {
                    for expected in values {
                        if entity_values.iter().any(|actual| actual == expected) {
                            return true;
                        }
                    }
                }
            }
        }

        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_multi_module_matching() {
        let loader = TagLoader::new(Some(PathBuf::from("../tags"))).unwrap();

        // Q41253 = movie theater, should match both commerce AND entertainment in places
        let result = loader.classify_by_p31(&["Q41253".to_string()]);

        println!("Category: {}", result.category);
        println!("Modules: {:?}", result.modules);

        assert_eq!(result.category, "places");
        assert!(
            result.modules.contains(&"commerce".to_string()),
            "Should match commerce, got: {:?}",
            result.modules
        );
        assert!(
            result.modules.contains(&"entertainment".to_string()),
            "Should match entertainment, got: {:?}",
            result.modules
        );
    }

    #[test]
    fn test_military_heritage_overlap() {
        let loader = TagLoader::new(Some(PathBuf::from("../tags"))).unwrap();

        // Q1785071 = fort, should match both military AND heritage
        let result = loader.classify_by_p31(&["Q1785071".to_string()]);

        println!("Category: {}", result.category);
        println!("Modules: {:?}", result.modules);

        assert_eq!(result.category, "places");
        assert!(
            result.modules.contains(&"military".to_string()),
            "Should match military, got: {:?}",
            result.modules
        );
        assert!(
            result.modules.contains(&"heritage".to_string()),
            "Should match heritage, got: {:?}",
            result.modules
        );
    }

    #[test]
    fn test_food_commerce_overlap() {
        let loader = TagLoader::new(Some(PathBuf::from("../tags"))).unwrap();

        // Q11707 = restaurant, should match both commerce AND food
        let result = loader.classify_by_p31(&["Q11707".to_string()]);

        println!("Category: {}", result.category);
        println!("Modules: {:?}", result.modules);

        assert_eq!(result.category, "places");
        assert!(
            result.modules.contains(&"commerce".to_string()),
            "Should match commerce, got: {:?}",
            result.modules
        );
        assert!(
            result.modules.contains(&"food".to_string()),
            "Should match food, got: {:?}",
            result.modules
        );
    }

    #[test]
    fn test_presence_indicator_matches_on_empty_values() {
        let loader = TagLoader::new(Some(PathBuf::from("../tags"))).unwrap();

        // creative_works_media gate via painting; authorship has presence indicators
        // such as P170 (creator). Even with unknown/no parsed value, key presence
        // should trigger the module.
        let entity = Entity {
            qid: "QTEST".to_string(),
            properties: HashMap::from([
                ("P31".to_string(), vec!["Q3305213".to_string()]), // painting
                ("P170".to_string(), vec![]),                      // creator present, value unknown
            ]),
        };

        let result = loader.classify(&entity);

        assert_eq!(result.category, "creative_works_media");
        assert!(
            result.modules.contains(&"authorship".to_string()),
            "Should match authorship via P170 presence, got: {:?}",
            result.modules
        );
    }
}
