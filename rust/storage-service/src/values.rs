//! The portable value grammar used by document, KV, vector, lexical and object
//! operations. Validation visits every node, but sends bounded node batches through
//! the host ABI. Plain strings are represented by their type, never copied merely
//! for validation; byte encodings and object references retain their actual data.

use crate::{StorageError, fields, object};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", content = "value", rename_all = "snake_case")]
pub enum StorageValue {
    Json(Value),
    Bytes(String),
    Mapping(BTreeMap<String, StorageValue>),
    Sequence(Vec<StorageValue>),
    Document(Document),
    ObjectReference(String),
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Document {
    pub id: String,
    pub revision: u64,
    pub value: Box<StorageValue>,
}

fn invalid() -> StorageError {
    StorageError::new("STORAGE_INVALID_PROCESS_VALUE")
}

fn base64(value: &str) -> bool {
    if value.is_empty() {
        return true;
    }
    if !value.len().is_multiple_of(4) || !value.is_ascii() {
        return false;
    }
    let bytes = value.as_bytes();
    let padding = bytes.iter().rev().take_while(|byte| **byte == b'=').count();
    padding <= 2
        && bytes[..bytes.len() - padding]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'+' || *byte == b'/')
}

fn blob_reference(value: &str) -> bool {
    let parts = value.split(':').collect::<Vec<_>>();
    parts.len() == 6
        && parts[0] == "blob"
        && parts[1] == "v1"
        && parts[2..5].iter().all(|part| {
            part.len() == 64
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        && (parts[5] == "0"
            || (!parts[5].starts_with('0')
                && !parts[5].is_empty()
                && parts[5].bytes().all(|byte| byte.is_ascii_digit())))
}

/// A flat node retains its tag, scalar validation data, depth and child count.
/// Container children follow as separate nodes; this prevents copying a large
/// document into one native request or relaxing the existing frame ceiling.
pub fn validate_node(value: &Value) -> Result<(), StorageError> {
    let node = object(value, "STORAGE_INVALID_PROCESS_VALUE")?;
    fields(
        node,
        &["depth", "type", "value"],
        &[],
        "STORAGE_INVALID_PROCESS_VALUE",
    )?;
    let depth = node["depth"].as_u64().ok_or_else(invalid)?;
    if depth > 32 {
        return Err(StorageError::new("STORAGE_PROCESS_LIMIT"));
    }
    let value = &node["value"];
    match node["type"].as_str() {
        Some("json")
            if value.is_null() || value.is_boolean() || value.is_number() || value.is_string() =>
        {
            Ok(())
        }
        Some("bytes") if value.as_str().is_some_and(base64) => Ok(()),
        Some("byte_chunk") => {
            let chunk = object(value, "STORAGE_INVALID_PROCESS_VALUE")?;
            fields(
                chunk,
                &["data", "last"],
                &[],
                "STORAGE_INVALID_PROCESS_VALUE",
            )?;
            if !chunk["last"].is_boolean()
                || !chunk["data"].as_str().is_some_and(|data| {
                    base64(data) && (chunk["last"] == true || !data.contains('='))
                })
            {
                Err(invalid())
            } else {
                Ok(())
            }
        }
        Some("mapping" | "sequence") => {
            if value.as_u64().is_some_and(|length| length <= 10000) {
                Ok(())
            } else {
                Err(StorageError::new("STORAGE_PROCESS_LIMIT"))
            }
        }
        Some("document") => {
            let document = object(value, "STORAGE_INVALID_PROCESS_VALUE")?;
            fields(
                document,
                &["id", "revision", "value_kind"],
                &[],
                "STORAGE_INVALID_PROCESS_VALUE",
            )?;
            if document["id"].is_string()
                && document["revision"]
                    .as_u64()
                    .is_some_and(|revision| revision > 0)
                && document["value_kind"].as_str() == Some("mapping")
            {
                Ok(())
            } else {
                Err(invalid())
            }
        }
        Some("object_reference") if value.as_str().is_some_and(blob_reference) => Ok(()),
        _ => Err(invalid()),
    }
}

impl StorageValue {
    pub fn validate(&self) -> Result<(), StorageError> {
        self.validate_at(0, &mut 250000)
    }

    fn validate_at(&self, depth: usize, remaining: &mut usize) -> Result<(), StorageError> {
        if depth > 32 || *remaining == 0 {
            return Err(StorageError::new("STORAGE_PROCESS_LIMIT"));
        }
        *remaining -= 1;
        match self {
            Self::Json(value) if value.is_array() || value.is_object() => Err(invalid()),
            Self::Bytes(value) if !base64(value) => Err(invalid()),
            Self::ObjectReference(value) if !blob_reference(value) => Err(invalid()),
            Self::Sequence(values) => {
                if values.len() > 10000 {
                    return Err(StorageError::new("STORAGE_PROCESS_LIMIT"));
                }
                for value in values {
                    value.validate_at(depth + 1, remaining)?;
                }
                Ok(())
            }
            Self::Mapping(values) => {
                if values.len() > 10000 {
                    return Err(StorageError::new("STORAGE_PROCESS_LIMIT"));
                }
                for value in values.values() {
                    value.validate_at(depth + 1, remaining)?;
                }
                Ok(())
            }
            Self::Document(document) => {
                if document.revision == 0 || !matches!(document.value.as_ref(), Self::Mapping(_)) {
                    return Err(invalid());
                }
                document.value.validate_at(depth + 1, remaining)
            }
            _ => Ok(()),
        }
    }
}
