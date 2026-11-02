//! Default agent services. The lifecycle runtime does not depend on this crate.
//!
//! A composition may use these loop/graph implementations or replace their
//! public services. This crate owns only per-execution control state; session
//! authority, tools, model calls and persistence belong to their own plugins.

pub mod agent_loop;
pub mod graph;

#[derive(Debug, serde::Serialize)]
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
