//! Public storage contracts, independent of the plugin runtime and database drivers.
//!
//! The host owns plugin lifetime. Drivers own connections and transaction execution.
//! This crate owns the portable capability, identity, value and receipt semantics;
//! it never retries a write or maintains a second transaction/accounting ledger.

pub mod contracts;
pub mod operations;
pub mod service;
pub mod transaction;
pub mod values;

use serde::Serialize;
use serde_json::{Map, Value, json};

pub const PROTOCOL: &str = "storage-service/1";

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct StorageError {
    pub code: String,
    pub message: String,
    #[serde(skip_serializing_if = "Map::is_empty")]
    pub details: Map<String, Value>,
}

impl StorageError {
    pub fn new(code: &str) -> Self {
        Self {
            code: code.into(),
            message: code.into(),
            details: Map::new(),
        }
    }

    pub fn commit_unknown(operation_id: &str) -> Self {
        let mut error = Self::new("COMMIT_UNKNOWN");
        error
            .details
            .insert("operation_id".into(), json!(operation_id));
        error
    }
}

impl std::fmt::Display for StorageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for StorageError {}

pub(crate) fn fields(
    object: &Map<String, Value>,
    required: &[&str],
    optional: &[&str],
    code: &str,
) -> Result<(), StorageError> {
    if required.iter().any(|key| !object.contains_key(*key))
        || object
            .keys()
            .any(|key| !required.contains(&key.as_str()) && !optional.contains(&key.as_str()))
    {
        return Err(StorageError::new(code));
    }
    Ok(())
}

pub(crate) fn object<'a>(
    value: &'a Value,
    code: &str,
) -> Result<&'a Map<String, Value>, StorageError> {
    value.as_object().ok_or_else(|| StorageError::new(code))
}

/// Adapter entry point for the existing native transport, outside the runtime kernel.
pub fn dispatch(command: &str, mut arguments: Map<String, Value>) -> Result<Value, StorageError> {
    if arguments
        .remove("protocol")
        .as_ref()
        .and_then(Value::as_str)
        != Some(PROTOCOL)
    {
        return Err(StorageError::new("STORAGE_PROTOCOL_VERSION_INVALID"));
    }
    match command {
        "storage_service" => Ok(service::reply(&arguments)),
        "storage_contract_validate" => {
            fields(
                &arguments,
                &["contract"],
                &["methods", "required"],
                "INVALID_STORAGE_CONTRACT",
            )?;
            let contract = contracts::Contract::parse(&arguments["contract"])?;
            if let Some(methods) = arguments.get("methods") {
                contract.validate_methods(methods)?;
            }
            let satisfied = arguments
                .get("required")
                .map(contracts::Contract::parse)
                .transpose()?
                .is_none_or(|required| contract.satisfies(&required));
            Ok(json!({"family":contract.family(), "satisfies":satisfied}))
        }
        "storage_identity_validate" => {
            fields(
                &arguments,
                &["identity"],
                &["expected"],
                "STORAGE_DATASET_IDENTITY_MISMATCH",
            )?;
            let actual = contracts::StorageIdentity::parse(&arguments["identity"])?;
            if let Some(expected) = arguments.get("expected")
                && actual != contracts::StorageIdentity::parse(expected)?
            {
                return Err(StorageError::new("STORAGE_DATASET_IDENTITY_MISMATCH"));
            }
            Ok(json!({"valid":true}))
        }
        "storage_invocation_validate" => {
            fields(
                &arguments,
                &["contract", "methods", "method", "argc", "keywords"],
                &[],
                "STORAGE_INVALID_PROCESS_REQUEST",
            )?;
            contracts::validate_invocation(&arguments)?;
            Ok(json!({"valid":true}))
        }
        "storage_structured_validate" => {
            fields(&arguments, &["header"], &[], "STORAGE_EXPRESSION_INVALID")?;
            transaction::validate_header(&arguments["header"])?;
            Ok(json!({"valid":true}))
        }
        "storage_commit_resolve" => {
            fields(
                &arguments,
                &["operation_id", "response"],
                &[],
                "DATABASE_OPERATION_ID_REQUIRED",
            )?;
            let operation = contracts::identifier(
                &arguments["operation_id"],
                "DATABASE_OPERATION_ID_REQUIRED",
            )?;
            Ok(serde_json::to_value(transaction::resolve_commit(
                operation,
                &arguments["response"],
            ))
            .expect("receipt is serializable"))
        }
        "storage_value_validate" => {
            fields(
                &arguments,
                &["values"],
                &[],
                "STORAGE_INVALID_PROCESS_VALUE",
            )?;
            let entries = arguments["values"]
                .as_array()
                .ok_or_else(|| StorageError::new("STORAGE_INVALID_PROCESS_VALUE"))?;
            for entry in entries {
                let value: values::StorageValue = serde_json::from_value(entry.clone())
                    .map_err(|_| StorageError::new("STORAGE_INVALID_PROCESS_VALUE"))?;
                value.validate()?;
            }
            Ok(json!({"valid":true,"nodes":entries.len()}))
        }
        _ => Err(StorageError::new("STORAGE_PROTOCOL_COMMAND_UNKNOWN")),
    }
}

#[cfg(test)]
mod tests;
