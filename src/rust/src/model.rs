use std::collections::HashMap;

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct CategoryConfig {
    #[serde(default)]
    pub gate_qids: Vec<String>,
    #[serde(default)]
    pub core: Option<serde_yaml::Value>,
    #[serde(default)]
    pub modules: HashMap<String, Module>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Module {
    #[serde(default, rename = "type")]
    pub module_type: Option<String>,
    #[serde(default)]
    pub indicators: HashMap<String, Option<Vec<String>>>,
    #[serde(default)]
    pub value_props: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct Entity {
    pub qid: String,
    pub properties: HashMap<String, Vec<String>>,
}

#[derive(Debug, Clone, Default)]
pub struct ClassificationResult {
    pub qid: String,
    pub category: String,
    pub gate_property: String,
    pub modules: Vec<String>,
    pub core_props: Vec<String>,
    pub module_value_props: HashMap<String, Vec<String>>,
}
