use std::{fmt, io};

#[derive(Debug)]
pub enum TagError {
    Io(io::Error),
    Yaml(serde_yaml::Error),
}

impl fmt::Display for TagError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TagError::Io(err) => write!(f, "I/O error: {}", err),
            TagError::Yaml(err) => write!(f, "YAML error: {}", err),
        }
    }
}

impl std::error::Error for TagError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            TagError::Io(err) => Some(err),
            TagError::Yaml(err) => Some(err),
        }
    }
}

impl From<io::Error> for TagError {
    fn from(err: io::Error) -> Self {
        TagError::Io(err)
    }
}

impl From<serde_yaml::Error> for TagError {
    fn from(err: serde_yaml::Error) -> Self {
        TagError::Yaml(err)
    }
}
