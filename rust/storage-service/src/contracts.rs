use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::{StorageError, fields, object};

pub const FAMILIES: [&str; 6] = [
    "relational",
    "document",
    "kv",
    "vector",
    "lexical",
    "object",
];

pub fn identifier<'a>(value: &'a Value, code: &str) -> Result<&'a str, StorageError> {
    value
        .as_str()
        .filter(|text| !text.trim().is_empty() && text.chars().count() <= 200)
        .ok_or_else(|| StorageError::new(code))
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SpaceKey {
    pub application_id: String,
    pub owner_id: String,
    pub space_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StorageIdentity {
    pub space: SpaceKey,
    pub dataset_id: String,
}

impl StorageIdentity {
    pub fn parse(value: &Value) -> Result<Self, StorageError> {
        let identity: Self = serde_json::from_value(value.clone())
            .map_err(|_| StorageError::new("STORAGE_DATASET_IDENTITY_MISMATCH"))?;
        for part in [
            &identity.space.application_id,
            &identity.space.owner_id,
            &identity.space.space_id,
            &identity.dataset_id,
        ] {
            identifier(
                &Value::String(part.clone()),
                "STORAGE_DATASET_IDENTITY_MISMATCH",
            )?;
        }
        Ok(identity)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Contract {
    pub id: String,
    pub major: u32,
    #[serde(default)]
    pub extends: Vec<Contract>,
}

impl Contract {
    pub fn parse(value: &Value) -> Result<Self, StorageError> {
        let selected: Self = serde_json::from_value(value.clone())
            .map_err(|_| StorageError::new("INVALID_STORAGE_CONTRACT"))?;
        selected.validate(&mut BTreeSet::new(), 0)?;
        Ok(selected)
    }

    fn validate(
        &self,
        ancestors: &mut BTreeSet<(String, u32)>,
        depth: usize,
    ) -> Result<(), StorageError> {
        let valid = self
            .id
            .starts_with(|character: char| character.is_ascii_lowercase())
            && self.id.chars().all(|character| {
                character.is_ascii_lowercase()
                    || character.is_ascii_digit()
                    || matches!(character, '_' | '-' | '.')
            });
        if !valid
            || self.major == 0
            || depth > 32
            || self.extends.len() > 64
            || !ancestors.insert((self.id.clone(), self.major))
        {
            return Err(StorageError::new("INVALID_STORAGE_CONTRACT"));
        }
        for parent in &self.extends {
            parent.validate(ancestors, depth + 1)?;
        }
        ancestors.remove(&(self.id.clone(), self.major));
        Ok(())
    }

    pub fn family(&self) -> &str {
        self.id
            .strip_prefix("storage.")
            .unwrap_or(&self.id)
            .split('.')
            .next()
            .unwrap_or("")
    }

    pub fn satisfies(&self, required: &Self) -> bool {
        (self.id == required.id && self.major == required.major)
            || self.extends.iter().any(|parent| parent.satisfies(required))
    }

    pub fn validate_methods(&self, methods: &Value) -> Result<(), StorageError> {
        let entries = method_names(methods)?;
        if crate::operations::catalogue(self)
            .keys()
            .any(|method| !entries.contains(method.as_str()))
        {
            return Err(StorageError::new("STORAGE_IMPLEMENTATION_INVALID"));
        }
        Ok(())
    }
}

fn method_names(value: &Value) -> Result<BTreeSet<&str>, StorageError> {
    let entries = value
        .as_array()
        .ok_or_else(|| StorageError::new("STORAGE_IMPLEMENTATION_INVALID"))?;
    let mut methods = BTreeSet::new();
    for entry in entries {
        let name = entry
            .as_str()
            .ok_or_else(|| StorageError::new("STORAGE_IMPLEMENTATION_INVALID"))?;
        if name.is_empty() || name.starts_with('_') || !methods.insert(name) {
            return Err(StorageError::new("STORAGE_IMPLEMENTATION_INVALID"));
        }
    }
    Ok(methods)
}

pub fn validate_invocation(arguments: &Map<String, Value>) -> Result<(), StorageError> {
    let contract = Contract::parse(&arguments["contract"])?;
    let name = arguments["method"]
        .as_str()
        .ok_or_else(|| StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"))?;
    let operation = crate::operations::catalogue(&contract)
        .remove(name)
        .ok_or_else(|| StorageError::new("STORAGE_PROCESS_METHOD_REJECTED"))?;
    let methods = method_names(&arguments["methods"])?;
    if !methods.contains(name) {
        return Err(StorageError::new("STORAGE_PROCESS_METHOD_REJECTED"));
    }
    let argc = arguments["argc"]
        .as_u64()
        .ok_or_else(|| StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"))?
        as usize;
    let keywords: Vec<String> = serde_json::from_value(arguments["keywords"].clone())
        .map_err(|_| StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"))?;
    operation.bind(argc, &keywords)?;
    Ok(())
}

pub fn validate_descriptor(value: &Value) -> Result<(), StorageError> {
    let value = object(value, "STORAGE_IMPLEMENTATION_INVALID")?;
    fields(
        value,
        &["identity", "contract", "methods"],
        &[],
        "STORAGE_IMPLEMENTATION_INVALID",
    )?;
    StorageIdentity::parse(&value["identity"])?;
    Contract::parse(&value["contract"])?.validate_methods(&value["methods"])
}
