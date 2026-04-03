//! Extract entities of a given category from classified parquet files into a single output file.

use std::fs::{self, File};
use std::path::PathBuf;

use anyhow::Result;
use arrow::array::{Scalar, StringArray};
use arrow::compute::kernels::cmp::eq;
use arrow::compute::{filter_record_batch, is_null};
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::WriterProperties;

#[derive(Parser, Debug)]
#[command(name = "extract_category")]
#[command(about = "Extract entities of a given category from classified parquet files")]
struct Args {
    /// Category to extract (e.g. science, places, creative_works_media, organizations, events_actions)
    #[arg(long, required_unless_present = "unclassified")]
    category: Option<String>,

    /// Extract unclassified entities (category IS NULL)
    #[arg(long, conflicts_with = "category")]
    unclassified: bool,

    /// Input directory with classified parquet files
    #[arg(long, default_value = "data_dump_output/classified_rust")]
    input_dir: PathBuf,

    /// Output parquet file (default: data_dump_output/entity_{category}.parquet)
    #[arg(long)]
    output: Option<PathBuf>,
}

fn main() -> Result<()> {
    let args = Args::parse();

    let label = if args.unclassified {
        "unclassified".to_string()
    } else {
        args.category.clone().unwrap()
    };

    let output = args
        .output
        .unwrap_or_else(|| PathBuf::from(format!("data_dump_output/entity_{}.parquet", label)));

    // Find all parquet files
    let mut files: Vec<PathBuf> = fs::read_dir(&args.input_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().map_or(false, |e| e == "parquet"))
        .filter(|p| {
            p.file_name()
                .map_or(false, |n| n.to_string_lossy().ends_with(".parquet"))
        })
        .collect();
    files.sort();

    if files.is_empty() {
        eprintln!("No parquet files found in {:?}", args.input_dir);
        std::process::exit(1);
    }

    println!("Extracting: {:?}", label);
    println!("Input directory: {:?}", args.input_dir);
    println!("Output: {:?}", output);
    println!("Files to process: {}", files.len());

    // Ensure output directory exists
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }

    // Read schema from first file
    let first_file = File::open(&files[0])?;
    let first_reader = ParquetRecordBatchReaderBuilder::try_new(first_file)?;
    let schema = first_reader.schema().clone();

    // Find category column index
    let category_idx = schema
        .index_of("category")
        .expect("Schema must have a 'category' column");

    // Create output writer
    let out_file = File::create(&output)?;
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(Default::default()))
        .build();
    let mut writer = ArrowWriter::try_new(out_file, schema.clone(), Some(props))?;

    // Progress bar
    let progress = ProgressBar::new(files.len() as u64);
    progress.set_style(ProgressStyle::default_bar().template(
        "[{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} files ({per_sec}, ETA {eta})",
    )?);

    let mut total_rows_read: u64 = 0;
    let mut total_category_rows: u64 = 0;

    for file_path in &files {
        let file = File::open(file_path)?;
        let reader = ParquetRecordBatchReaderBuilder::try_new(file)?
            .with_batch_size(65536)
            .build()?;

        for batch_result in reader {
            let batch = batch_result?;
            let num_rows = batch.num_rows();
            total_rows_read += num_rows as u64;

            // Build boolean mask
            let category_col = batch.column(category_idx);
            let mask = if args.unclassified {
                is_null(category_col)?
            } else {
                let cat_scalar = Scalar::new(StringArray::from_iter_values([label.as_str()]));
                eq(category_col, &cat_scalar)?
            };

            // Filter the batch
            let filtered = filter_record_batch(&batch, &mask)?;

            if filtered.num_rows() > 0 {
                total_category_rows += filtered.num_rows() as u64;
                writer.write(&filtered)?;
            }
        }

        progress.inc(1);
    }

    writer.close()?;
    progress.finish_with_message("Done");

    println!(
        "\nTotal rows read: {}\n{} rows written: {}\nOutput: {:?}",
        total_rows_read, label, total_category_rows, output
    );

    Ok(())
}
