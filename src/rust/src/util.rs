use once_cell::sync::Lazy;
use regex::Regex;

static QID_PATTERN: Lazy<Regex> = Lazy::new(|| Regex::new(r"^Q\d+$").expect("valid QID regex"));
static PID_PATTERN: Lazy<Regex> = Lazy::new(|| Regex::new(r"^P\d+$").expect("valid PID regex"));

pub fn is_valid_qid(s: &str) -> bool {
    QID_PATTERN.is_match(s)
}

pub fn is_valid_pid(s: &str) -> bool {
    PID_PATTERN.is_match(s)
}
