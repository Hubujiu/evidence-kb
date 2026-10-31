mod coordinator;
pub mod manifest;
pub mod native;
mod protocol;

pub use coordinator::Coordinator;
pub use manifest::Manifest;
pub use protocol::{Action, Binding, Command, Completion, Desired, Phase};
use serde::Serialize;

pub const PROTOCOL: &str = "plugin-runtime/1";

#[derive(Clone, Debug, Serialize)]
pub struct Error {
    pub code: String,
    pub message: String,
}

impl Error {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for Error {}

#[cfg(test)]
mod tests;
