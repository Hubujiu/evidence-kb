use crate::contracts::{Contract, FAMILIES, StorageIdentity};
use crate::transaction::{CommitOutcome, resolve_commit};
use crate::values::{CodecLimits, StorageValue, ValueCodec};
use crate::{PROTOCOL, dispatch};
use serde_json::{Value, json};

fn call(command: &str, value: Value) -> Result<Value, crate::StorageError> {
    let mut value = value.as_object().unwrap().clone();
    value.insert("protocol".into(), json!(PROTOCOL));
    dispatch(command, value)
}

fn identity(dataset: &str) -> Value {
    json!({"space":{"application_id":"app", "owner_id":"sessions", "space_id":"primary"}, "dataset_id":dataset})
}

fn header(value: Value) -> Value {
    let payloads = ["statement", "values", "updates", "returning"];
    let source = value.as_object().unwrap();
    json!({"fields":source.keys().collect::<Vec<_>>(), "control":source.iter()
        .filter(|(key, _)| !payloads.contains(&key.as_str())).map(|(key, value)| (key.clone(),value.clone()))
        .collect::<serde_json::Map<_,_>>()})
}

#[test]
fn all_six_families_have_versioned_contracts_and_explicit_extensions() {
    for family in FAMILIES {
        let root = json!({"id":format!("storage.{family}"),"major":1});
        let required = Contract::parse(&root).unwrap();
        let extension = Contract::parse(&json!({"id":format!("storage.{family}.vendor"),"major":1,
            "extends":[root]}))
        .unwrap();
        assert_eq!(extension.family(), family);
        assert!(extension.satisfies(&required));
        assert!(!required.satisfies(&extension));
        assert!(!extension.satisfies(&Contract {
            major: 2,
            ..required
        }));
    }
    assert!(
        Contract::parse(&json!({"id":"storage.kv", "major":1,
        "extends":[{"id":"storage.kv", "major":1}]}))
        .is_err()
    );
}

#[test]
fn methods_are_checked_for_inherited_capabilities_without_provider_names() {
    let contract = json!({"id":"example.redis", "major":1,"extends":[{"id":"storage.kv.expiring",
        "major":1, "extends":[{"id":"storage.kv", "major":1}]}]});
    let result = call(
        "storage_contract_validate",
        json!({"contract":contract,
        "methods":["get","set","delete","put_expiring"]}),
    )
    .unwrap();
    assert_eq!(result["satisfies"], true);
    assert_eq!(
        call(
            "storage_contract_validate",
            json!({"contract":contract,
        "methods":["get","set","delete"]})
        )
        .unwrap_err()
        .code,
        "STORAGE_IMPLEMENTATION_INVALID"
    );
}

#[test]
fn all_identity_fields_are_required_and_dataset_replacement_is_detected() {
    let actual = identity("one");
    assert!(StorageIdentity::parse(&actual).is_ok());
    for field in ["application_id", "owner_id", "space_id"] {
        let mut changed = actual.clone();
        changed["space"][field] = json!("other");
        assert!(
            call(
                "storage_identity_validate",
                json!({"identity":actual, "expected":changed})
            )
            .is_err()
        );
    }
    assert_eq!(
        call(
            "storage_identity_validate",
            json!({"identity":actual, "expected":identity("two")})
        )
        .unwrap_err()
        .code,
        "STORAGE_DATASET_IDENTITY_MISMATCH"
    );
    for invalid in [json!(""), json!(true), Value::Null] {
        let mut changed = actual.clone();
        changed["dataset_id"] = invalid;
        assert!(StorageIdentity::parse(&changed).is_err());
    }
}

#[test]
fn finite_invocations_cannot_enter_private_or_transaction_methods() {
    for method in ["close", "_engine", "prepare", "transaction", "guard"] {
        assert_eq!(
            call(
                "storage_invocation_validate",
                json!({"contract":{"id":"storage.kv","major":1},
            "methods":["get","set","delete",method],"method":method,"argc":0,"keywords":[]})
            )
            .unwrap_err()
            .code,
            "STORAGE_PROCESS_METHOD_REJECTED"
        );
    }
    assert!(
        call(
            "storage_invocation_validate",
            json!({"contract":{"id":"storage.document","major":1},
        "methods":["get","put"],"method":"get","argc":0,"keywords":["document_id"]})
        )
        .is_ok()
    );
}

#[test]
fn transaction_controls_reject_extra_fields_false_integers_and_unbounded_limits() {
    let request = json!({"version":1,"op":"begin","write":true,"scope":{"tenant":"one"},
        "limits":{"max_rows":10000,"max_bytes":8388608,"max_statements":20000,"max_seconds":30},
        "operation_id":"physical-operation"});
    assert!(
        call(
            "storage_structured_validate",
            json!({"header":header(request.clone())})
        )
        .is_ok()
    );
    for (field, value) in [
        ("max_rows", json!(true)),
        ("max_bytes", json!(8388609)),
        ("max_seconds", json!(301)),
    ] {
        let mut changed = request.clone();
        changed["limits"][field] = value;
        assert!(
            call(
                "storage_structured_validate",
                json!({"header":header(changed)})
            )
            .is_err()
        );
    }
    let mut changed = request.clone();
    changed["dataset_id"] = json!("another-dataset");
    assert_eq!(
        call(
            "storage_structured_validate",
            json!({"header":header(changed)})
        )
        .unwrap_err()
        .code,
        "STORAGE_EXPRESSION_INVALID"
    );
    let mut changed = request;
    changed["version"] = json!(true);
    assert_eq!(
        call(
            "storage_structured_validate",
            json!({"header":header(changed)})
        )
        .unwrap_err()
        .code,
        "STORAGE_WIRE_VERSION_INVALID"
    );
}

#[test]
fn large_payloads_do_not_expand_native_control_messages() {
    let request = json!({"version":1,"op":"execute","handle":"transaction",
        "statement":{"kind":"select","literal":"x".repeat(5*1024*1024)}});
    let metadata = header(request);
    assert!(serde_json::to_vec(&metadata).unwrap().len() < 256);
    assert!(call("storage_structured_validate", json!({"header":metadata})).is_ok());
}

#[test]
fn commit_evidence_never_infers_success_rollback_or_retry_from_unknown() {
    for response in [
        Value::Null,
        json!({}),
        json!({"status":"UNKNOWN","operation_id":"op"}),
        json!({"status":"COMMITTED","operation_id":"other"}),
        json!({"status":"RELEASED","operation_id":"op"}),
    ] {
        let receipt = resolve_commit("op", &response);
        assert_eq!(receipt.status, CommitOutcome::Unknown);
        assert_eq!(receipt.operation_id, "op");
        assert!(!receipt.replay_allowed);
    }
    assert_eq!(
        resolve_commit("op", &json!({"status":"COMMITTED","operation_id":"op"})).status,
        CommitOutcome::Committed
    );
    assert_eq!(
        resolve_commit("op", &json!({"status":"ROLLED_BACK","operation_id":"op"})).status,
        CommitOutcome::RolledBack
    );
}

#[test]
fn portable_values_preserve_literal_tags_documents_and_binary() {
    let wire = json!({"type":"document","value":{"id":"record","revision":2,"value":{"type":"mapping",
        "value":{"type":{"type":"json","value":"bytes"},"binary":{"type":"bytes","value":"AP8="}}}}});
    let value: StorageValue = serde_json::from_value(wire.clone()).unwrap();
    value.validate().unwrap();
    assert_eq!(serde_json::to_value(value).unwrap(), wire);
    for value in [
        json!({"type":"bytes","value":"?"}),
        json!({"type":"json","value":{}}),
    ] {
        let value: StorageValue = serde_json::from_value(value).unwrap();
        assert!(value.validate().is_err());
    }
}

#[test]
fn streamed_codec_validates_full_structure_and_actual_scalar_values() {
    let mut codec = ValueCodec::new(CodecLimits::default()).unwrap();
    for event in [
        json!({"event":"open","type":"mapping","length":1}),
        json!({"event":"key","value":"payload"}),
        json!({"event":"open","type":"bytes"}),
        json!({"event":"chunk","value":"AP8="}),
        json!({"event":"close"}),
        json!({"event":"close"}),
    ] {
        codec.push(&event).unwrap();
    }
    assert_eq!(codec.finish().unwrap()[0].length, 1);
    let mut codec = ValueCodec::new(CodecLimits::default()).unwrap();
    codec.push(&json!({"event":"open","type":"bytes"})).unwrap();
    codec
        .push(&json!({"event":"chunk","value":"AP8="}))
        .unwrap();
    assert!(
        codec
            .push(&json!({"event":"chunk","value":"AAAA"}))
            .is_err()
    );
    let mut codec = ValueCodec::new(CodecLimits::default()).unwrap();
    codec
        .push(&json!({"event":"open","type":"sequence","length":1}))
        .unwrap();
    assert!(codec.push(&json!({"event":"close"})).is_err());
    let mut codec = ValueCodec::new(CodecLimits::default()).unwrap();
    assert!(
        codec
            .push(&json!({"event":"scalar","type":"integer","value":"00"}))
            .is_err()
    );
    for value in [
        json!({"type":"json","value":"正文\nquoted\""}),
        json!({"type":"document","value":{"id":"one","revision":2,"value":{"type":"mapping","value":{"data":{"type":"bytes","value":"AP8="}}}}}),
    ] {
        let typed: StorageValue = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(
            typed.inspect(CodecLimits::default()).unwrap().encoded_bytes,
            serde_json::to_vec(&value).unwrap().len()
        );
    }
}

#[test]
fn operation_catalogue_is_shared_by_native_admission_and_independent_rust_drivers() {
    use crate::service::{Descriptor, Driver, Service};
    use std::cell::Cell;
    use std::collections::BTreeMap;
    use std::rc::Rc;
    struct Kv {
        calls: Rc<Cell<usize>>,
        result: StorageValue,
    }
    impl Driver for Kv {
        fn descriptor(&self) -> Descriptor {
            Descriptor {
                identity: StorageIdentity::parse(&identity("dataset")).unwrap(),
                contract: Contract::parse(&json!({"id":"storage.kv","major":1})).unwrap(),
                methods: vec!["get".into(), "set".into(), "delete".into()],
            }
        }
        fn invoke(
            &mut self,
            _: &str,
            _: Vec<StorageValue>,
            _: BTreeMap<String, StorageValue>,
        ) -> Result<StorageValue, crate::StorageError> {
            self.calls.set(self.calls.get() + 1);
            Ok(self.result.clone())
        }
    }
    let calls = Rc::new(Cell::new(0));
    let mut service = Service::new(Kv {
        calls: calls.clone(),
        result: StorageValue::Json(Value::Null),
    })
    .unwrap();
    assert!(
        service
            .invoke(
                "set",
                vec![
                    StorageValue::Json(json!("key")),
                    StorageValue::Json(json!("not binary"))
                ],
                BTreeMap::new()
            )
            .is_err()
    );
    assert_eq!(calls.get(), 0);
    service
        .invoke(
            "set",
            vec![],
            BTreeMap::from([
                ("key".into(), StorageValue::Json(json!("key"))),
                ("value".into(), StorageValue::Bytes("AP8=".into())),
            ]),
        )
        .unwrap();
    assert_eq!(calls.get(), 1);
    let calls = Rc::new(Cell::new(0));
    let mut bad = Service::new(Kv {
        calls: calls.clone(),
        result: StorageValue::Json(json!("invalid receipt")),
    })
    .unwrap();
    let error = bad
        .invoke(
            "set",
            vec![
                StorageValue::Json(json!("key")),
                StorageValue::Bytes("AP8=".into()),
            ],
            BTreeMap::new(),
        )
        .unwrap_err();
    assert_eq!(error.code, "STORAGE_WRITE_RESULT_UNAVAILABLE");
    assert_eq!(error.details["outcome"], "COMPLETED");
    assert_eq!(calls.get(), 1);
    let descriptor = service.descriptor().clone();
    let reply = call("storage_service", json!({"action":"open","descriptor":descriptor,"method":"set","argc":1,"keywords":[],
        "limits":{"request":{"max_nodes":100,"max_bytes":4096},"response":{"max_nodes":100,"max_bytes":4096}}})).unwrap();
    assert_eq!(reply["ok"], false);
    assert_eq!(reply["error"]["code"], "STORAGE_INVALID_PROCESS_REQUEST");
}
