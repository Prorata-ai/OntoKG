use std::path::PathBuf;
use std::process;

use clap::Parser;
use tags::TagValidator;

#[derive(Parser)]
#[command(name = "tag_validator", about = "Validate tag configuration YAML files")]
struct Args {
    /// Overlap threshold (0.0-1.0) to trigger warnings
    #[arg(long, short = 'o', default_value = "0.5")]
    overlap_threshold: f64,

    /// Require gate_qids to be exclusive across categories (duplicates become errors)
    #[arg(long)]
    gate_exclusive: bool,

    /// Show all errors and warnings even when validation passes
    #[arg(long, short)]
    verbose: bool,

    /// Path to tags directory
    #[arg(long)]
    tags_dir: Option<PathBuf>,
}

fn resolve_tags_dir(explicit: Option<PathBuf>) -> PathBuf {
    if let Some(dir) = explicit {
        return dir;
    }
    // Try common locations relative to CWD
    for candidate in &["src/tags", "../tags"] {
        let p = PathBuf::from(candidate);
        if p.is_dir() {
            return p;
        }
    }
    PathBuf::from("src/tags")
}

fn main() {
    let args = Args::parse();
    let tags_dir = resolve_tags_dir(args.tags_dir);

    let validator = match TagValidator::new(Some(tags_dir)) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("Failed to load tag files: {}", e);
            process::exit(1);
        }
    };

    let result = validator.validate(args.overlap_threshold, args.gate_exclusive);

    println!("{}", result.summary());
    println!();

    if args.verbose || !result.is_valid() {
        if !result.errors.is_empty() {
            println!("Errors:");
            for err in &result.errors {
                println!("  {}", err);
            }
            println!();
        }

        if !result.warnings.is_empty() {
            println!("Warnings:");
            for warn in &result.warnings {
                println!("  {}", warn);
            }
        }
    }

    if !result.is_valid() {
        process::exit(1);
    }
}
