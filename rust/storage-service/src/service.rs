use crate::{
    StorageError,
    contracts::{Contract, StorageIdentity},
    values::StorageValue,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Descriptor {
    pub identity: StorageIdentity,
    pub contract: Contract,
    pub methods: Vec<String>,
}

/// The implementation can use any backend. Connections, retries and resource
/// lifetime are deliberately not provided by this public contract adapter.
pub trait Driver {
    fn descriptor(&self) -> Descriptor;
    fn invoke(
        &mut self,
        method: &str,
        args: Vec<StorageValue>,
        kwargs: BTreeMap<String, StorageValue>,
    ) -> Result<StorageValue, StorageError>;
}

pub struct Service<D: Driver> {
    driver: D,
    descriptor: Descriptor,
}

impl<D: Driver> Service<D> {
    pub fn new(driver: D) -> Result<Self, StorageError> {
        let descriptor = driver.descriptor();
        crate::contracts::validate_descriptor(
            &serde_json::to_value(&descriptor).expect("descriptor is serializable"),
        )?;
        Ok(Self { driver, descriptor })
    }

    pub fn descriptor(&self) -> &Descriptor {
        &self.descriptor
    }

    pub fn invoke(
        &mut self,
        method: &str,
        args: Vec<StorageValue>,
        kwargs: BTreeMap<String, StorageValue>,
    ) -> Result<StorageValue, StorageError> {
        if self.driver.descriptor() != self.descriptor {
            return Err(StorageError::new("STORAGE_BINDING_CHANGED"));
        }
        let admission = json!({"contract":self.descriptor.contract, "methods":self.descriptor.methods,
            "method":method,"argc":args.len(),"keywords":kwargs.keys().collect::<Vec<_>>()});
        crate::contracts::validate_invocation(admission.as_object().expect("object"))?;
        for value in args.iter().chain(kwargs.values()) {
            value.validate()?;
        }
        let result = self.driver.invoke(method, args, kwargs)?;
        if let Err(error) = result.validate() {
            if matches!(
                method,
                "put" | "set" | "delete" | "put_expiring" | "stage" | "seal" | "batch_json"
            ) {
                let mut completed = StorageError::new("STORAGE_WRITE_RESULT_UNAVAILABLE");
                completed
                    .details
                    .insert("outcome".into(), json!("COMPLETED"));
                completed.details.insert("method".into(), json!(method));
                return Err(completed);
            }
            return Err(error);
        }
        Ok(result)
    }
}
