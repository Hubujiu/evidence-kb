pub mod agent_loop;
pub mod graph;
mod host;
mod manifest;

use serde::Serialize;
use serde_json::{Value, json};
use std::sync::{Mutex, OnceLock};

pub use host::HostRegistry;
pub use manifest::Manifest;

pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_REQUEST_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Serialize)]
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
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for Error {}

fn dispatch(request: Value) -> Result<Value, Error> {
    let Value::Object(mut object) = request else {
        return Err(Error::new("invalid_request", "request must be an object"));
    };
    if object.remove("version").and_then(|v| v.as_u64()) != Some(PROTOCOL_VERSION) {
        return Err(Error::new(
            "protocol_version",
            "unsupported protocol version",
        ));
    }
    let command = object
        .get("command")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::new("invalid_request", "command must be a string"))?
        .to_owned();
    if command.starts_with("loop_") {
        object.remove("command");
        return agent_loop::dispatch(&command, object);
    }
    if command.starts_with("graph_") {
        object.remove("command");
        return graph::dispatch(&command, object);
    }
    static HOSTS: OnceLock<Mutex<HostRegistry>> = OnceLock::new();
    HOSTS
        .get_or_init(|| Mutex::new(HostRegistry::default()))
        .lock()
        .map_err(|_| Error::new("host_poisoned", "native host lock is poisoned"))?
        .request(Value::Object(object))
}

pub fn call(bytes: &[u8]) -> Value {
    let result = if bytes.len() > MAX_REQUEST_BYTES {
        Err(Error::new(
            "request_too_large",
            "request exceeds protocol limit",
        ))
    } else {
        serde_json::from_slice(bytes)
            .map_err(|error| Error::new("invalid_json", error.to_string()))
            .and_then(dispatch)
    };
    match result {
        Ok(result) => json!({"ok": true, "result": result}),
        Err(error) => json!({"ok": false, "error": error}),
    }
}

#[cfg(test)]
mod host_tests;
