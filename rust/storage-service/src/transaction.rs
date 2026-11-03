use std::collections::BTreeSet;

use serde::Serialize;
use serde_json::Value;

use crate::contracts::identifier;
use crate::{StorageError, fields, object};

pub fn validate_header(value: &Value) -> Result<(), StorageError> {
    let header = object(value, "STORAGE_EXPRESSION_INVALID")?;
    fields(
        header,
        &["fields", "control"],
        &[],
        "STORAGE_EXPRESSION_INVALID",
    )?;
    let control = object(&header["control"], "STORAGE_EXPRESSION_INVALID")?;
    if control.get("version").and_then(Value::as_u64) != Some(1) {
        return Err(StorageError::new("STORAGE_WIRE_VERSION_INVALID"));
    }
    let operation = control
        .get("op")
        .and_then(Value::as_str)
        .ok_or_else(|| StorageError::new("STORAGE_WIRE_OPERATION_UNSUPPORTED"))?;
    let expected: &[&str] = match operation {
        "schemas" | "seal" => &["version", "op"],
        "prepare" => &["version", "op", "owner", "schema_version", "digest"],
        "begin" => &["version", "op", "write", "scope", "limits", "operation_id"],
        "guard" => &["version", "op", "name", "nowait"],
        "execute" => &["version", "op", "handle", "statement"],
        "insert" => &[
            "version",
            "op",
            "handle",
            "table",
            "values",
            "conflict_keys",
            "updates",
            "returning",
        ],
        "ping" => &["version", "op", "handle"],
        "finish" => &["version", "op", "handle", "commit"],
        _ => return Err(StorageError::new("STORAGE_WIRE_OPERATION_UNSUPPORTED")),
    };
    let names = header["fields"]
        .as_array()
        .ok_or_else(|| StorageError::new("STORAGE_EXPRESSION_INVALID"))?;
    let actual = names
        .iter()
        .filter_map(Value::as_str)
        .collect::<BTreeSet<_>>();
    if actual.len() != names.len() || actual != expected.iter().copied().collect() {
        return Err(StorageError::new("STORAGE_EXPRESSION_INVALID"));
    }
    let payloads = ["statement", "values", "updates", "returning"];
    let retained = expected
        .iter()
        .copied()
        .filter(|field| !payloads.contains(field))
        .collect::<Vec<_>>();
    fields(control, &retained, &[], "STORAGE_EXPRESSION_INVALID")?;
    match operation {
        "prepare" => {
            if !control["owner"].is_string()
                || control["schema_version"].as_u64().is_none()
                || !control["digest"].is_string()
            {
                return Err(StorageError::new("STORAGE_WIRE_SCHEMA_UNDECLARED"));
            }
        }
        "begin" => {
            if !control["write"].is_boolean() || !control["scope"].is_object() {
                return Err(StorageError::new("STORAGE_WIRE_TRANSACTION_INVALID"));
            }
            identifier(&control["operation_id"], "STORAGE_WIRE_TRANSACTION_INVALID")?;
            validate_limits(&control["limits"])?;
        }
        "guard" => {
            if !control["name"].is_string() || !control["nowait"].is_boolean() {
                return Err(StorageError::new("INVALID_STORAGE_GUARD"));
            }
        }
        "execute" | "insert" | "ping" | "finish" => {
            if !control["handle"].is_string() {
                return Err(StorageError::new("STORAGE_HANDLE_INVALID"));
            }
            if operation == "finish" && !control["commit"].is_boolean() {
                return Err(StorageError::new("STORAGE_WIRE_TRANSACTION_INVALID"));
            }
            if operation == "insert"
                && (!control["table"].is_string()
                    || !control["conflict_keys"]
                        .as_array()
                        .is_some_and(|keys| keys.iter().all(Value::is_string)))
            {
                return Err(StorageError::new("INVALID_STRUCTURED_CONFLICT_KEYS"));
            }
        }
        _ => {}
    }
    Ok(())
}

pub fn validate_limits(value: &Value) -> Result<(), StorageError> {
    let limits = object(value, "INVALID_DATABASE_LIMITS")?;
    fields(
        limits,
        &[],
        &["max_rows", "max_bytes", "max_statements", "max_seconds"],
        "STORAGE_EXPRESSION_INVALID",
    )?;
    for (key, maximum) in [
        ("max_rows", 10000),
        ("max_bytes", 8 * 1024 * 1024),
        ("max_statements", 20000),
    ] {
        if let Some(value) = limits.get(key) {
            let number = value
                .as_u64()
                .filter(|number| *number > 0)
                .ok_or_else(|| StorageError::new("INVALID_DATABASE_LIMITS"))?;
            if number > maximum {
                return Err(StorageError::new("DATABASE_QUERY_LIMIT"));
            }
        }
    }
    if let Some(value) = limits.get("max_seconds")
        && !value
            .as_f64()
            .is_some_and(|seconds| seconds.is_finite() && seconds > 0.0 && seconds <= 300.0)
    {
        return Err(StorageError::new("INVALID_DATABASE_LIMITS"));
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CommitOutcome {
    Committed,
    RolledBack,
    Unknown,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct CommitReceipt {
    pub operation_id: String,
    pub status: CommitOutcome,
    pub replay_allowed: bool,
}

pub fn resolve_commit(operation_id: &str, response: &Value) -> CommitReceipt {
    let status = if response.get("operation_id").and_then(Value::as_str) == Some(operation_id) {
        match response.get("status").and_then(Value::as_str) {
            Some("COMMITTED") => CommitOutcome::Committed,
            Some("ROLLED_BACK") => CommitOutcome::RolledBack,
            _ => CommitOutcome::Unknown,
        }
    } else {
        CommitOutcome::Unknown
    };
    CommitReceipt {
        operation_id: operation_id.into(),
        status,
        replay_allowed: false,
    }
}
