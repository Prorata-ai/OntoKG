use std::collections::HashMap;
use std::path::PathBuf;

use tags::{Entity, TagLoader, TagValidator};

fn props(pairs: &[(&str, &[&str])]) -> HashMap<String, Vec<String>> {
    let mut map = HashMap::new();
    for (pid, values) in pairs {
        map.insert(
            (*pid).to_string(),
            values.iter().map(|v| (*v).to_string()).collect(),
        );
    }
    map
}

fn main() {
    let tags_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../tags");

    let loader = TagLoader::new(Some(tags_dir.clone())).expect("load tags");

    println!("============================================================");
    println!("Loaded Categories:");
    println!("============================================================");
    for cat in loader.get_category_names() {
        let modules = loader.get_modules(&cat);
        println!("  {}: {} modules", cat, modules.len());
    }
    println!();

    let examples = vec![
        (
            "Barack Obama (person, politician)",
            Entity {
                qid: "Q76".to_string(),
                properties: props(&[
                    ("P31", &["Q5"]),
                    ("P39", &["Q11696"]),
                    ("P102", &["Q29552"]),
                ]),
            },
        ),
        (
            "Taylor Swift (person, entertainer)",
            Entity {
                qid: "Q26876".to_string(),
                properties: props(&[
                    ("P31", &["Q5"]),
                    ("P345", &["nm2357847"]),
                    ("P1953", &["252895"]),
                    ("P136", &["Q37073"]),
                ]),
            },
        ),
        (
            "Python (programming language)",
            Entity {
                qid: "Q28865".to_string(),
                properties: props(&[("P31", &["Q9143"]), ("P178", &["Q170539"])]),
            },
        ),
        (
            "Aspirin (drug)",
            Entity {
                qid: "Q18216".to_string(),
                properties: props(&[
                    ("P31", &["Q12140"]),
                    ("P274", &["C9H8O4"]),
                    ("P231", &["50-78-2"]),
                ]),
            },
        ),
        (
            "Inception (film)",
            Entity {
                qid: "Q25188".to_string(),
                properties: props(&[
                    ("P31", &["Q11424"]),
                    ("P57", &["Q25191"]),
                    ("P2130", &["+160000000"]),
                    ("P2142", &["+836800000"]),
                ]),
            },
        ),
    ];

    println!("============================================================");
    println!("Entity Classification Examples:");
    println!("============================================================");

    for (name, entity) in examples {
        let result = loader.classify(&entity);
        println!("\n{}", name);
        println!("  QID: {}", result.qid);
        println!("  Category: {}", result.category);
        println!("  Modules: {:?}", result.modules);
    }

    println!();
    println!("============================================================");
    println!("Quick Classification by P31 Only:");
    println!("============================================================");

    let quick_tests = vec![
        (vec!["Q5"], "human"),
        (vec!["Q11424"], "film"),
        (vec!["Q7889"], "video game"),
        (vec!["Q8142"], "currency"),
        (vec!["Q12136"], "disease"),
        (vec!["Q3918"], "university"),
    ];

    for (values, description) in quick_tests {
        let p31_values: Vec<String> = values.iter().map(|v| (*v).to_string()).collect();
        let result = loader.classify_by_p31(&p31_values);
        println!(
            "  P31={:?} ({}) -> category: {}",
            p31_values, description, result.category
        );
    }

    println!();
    println!("============================================================");
    println!("Validation:");
    println!("============================================================");

    let validator = TagValidator::new(Some(tags_dir)).expect("create validator");
    let validation_result = validator.validate(0.5);

    println!("{}", validation_result.summary());
    println!("  Stats:");
    for (k, v) in validation_result.stats {
        println!("    {}: {}", k, v);
    }
}
