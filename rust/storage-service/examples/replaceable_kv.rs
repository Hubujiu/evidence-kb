use serde_json::{Value, json};
use std::collections::BTreeMap;
use storage_service::{
    StorageError,
    contracts::{Contract, StorageIdentity},
    service::{Descriptor, Driver, Service},
    values::StorageValue,
};

struct MemoryKv {
    dataset: String,
    entries: BTreeMap<String, StorageValue>,
}

impl Driver for MemoryKv {
    fn descriptor(&self) -> Descriptor {
        Descriptor {
            identity: StorageIdentity::parse(&json!({"space":{"application_id":"example",
                "owner_id":"cache","space_id":"primary"}, "dataset_id":self.dataset}))
            .unwrap(),
            contract: Contract::parse(&json!({"id":"storage.kv","major":1})).unwrap(),
            methods: ["get", "set", "delete"]
                .into_iter()
                .map(str::to_string)
                .collect(),
        }
    }

    fn invoke(
        &mut self,
        method: &str,
        args: Vec<StorageValue>,
        _: BTreeMap<String, StorageValue>,
    ) -> Result<StorageValue, StorageError> {
        let Some(StorageValue::Json(Value::String(key))) = args.first() else {
            return Err(StorageError::new("STORAGE_INVALID_KEY"));
        };
        match method {
            "get" => Ok(self
                .entries
                .get(key)
                .cloned()
                .unwrap_or(StorageValue::Json(Value::Null))),
            "set" if args.len() == 2 && matches!(args[1], StorageValue::Bytes(_)) => {
                self.entries.insert(key.clone(), args[1].clone());
                Ok(StorageValue::Json(Value::Null))
            }
            "delete" => Ok(StorageValue::Json(json!(
                self.entries.remove(key).is_some()
            ))),
            _ => Err(StorageError::new("STORAGE_INVALID_PROCESS_REQUEST")),
        }
    }
}

fn consumer<D: Driver>(service: &mut Service<D>) -> Result<(), StorageError> {
    service.invoke(
        "set",
        vec![
            StorageValue::Json(json!("key")),
            StorageValue::Bytes("AP8=".into()),
        ],
        BTreeMap::new(),
    )?;
    assert_eq!(
        service.invoke(
            "get",
            vec![StorageValue::Json(json!("key"))],
            BTreeMap::new()
        )?,
        StorageValue::Bytes("AP8=".into())
    );
    Ok(())
}

fn main() -> Result<(), StorageError> {
    for dataset in ["provider-a-dataset", "provider-b-dataset"] {
        let mut provider = Service::new(MemoryKv {
            dataset: dataset.into(),
            entries: BTreeMap::new(),
        })?;
        consumer(&mut provider)?;
    }
    println!("two independent datasets satisfy the unchanged KV consumer");
    Ok(())
}
