pub mod agent_loop;
pub mod graph;

use serde_json::{Value, json};
use std::sync::{Mutex, OnceLock};

pub use plugin_runtime::{Error, Manifest};

pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_REQUEST_BYTES: usize = 4 * 1024 * 1024;

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
    if !command.starts_with("runtime_") {
        return Err(Error::new(
            "unknown_command",
            "lifecycle commands use runtime_* action protocol",
        ));
    }
    static HOSTS: OnceLock<Mutex<plugin_runtime::Coordinator>> = OnceLock::new();
    HOSTS
        .get_or_init(|| Mutex::new(plugin_runtime::Coordinator::default()))
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
mod protocol_tests {
    use super::*;

    #[test]
    fn abi_rejects_invalid_json_version_fields_and_oversized_requests() {
        assert_eq!(call(b"{")["error"]["code"], "invalid_json");
        assert_eq!(call(b"[]")["error"]["code"], "invalid_request");
        assert_eq!(
            call(br#"{"version":2,"command":"runtime_inspect","host":1}"#)["error"]["code"],
            "protocol_version"
        );
        assert_eq!(
            call(br#"{"version":1,"command":"runtime_create","protocol":"plugin-runtime/1","plugins":[],"extra":true}"#)["error"]["code"],
            "invalid_request"
        );
        assert_eq!(
            call(&vec![b' '; MAX_REQUEST_BYTES + 1])["error"]["code"],
            "request_too_large"
        );
    }
}
