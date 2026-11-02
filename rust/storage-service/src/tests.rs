use crate::contracts::{Contract, FAMILIES, StorageIdentity};
use crate::transaction::{CommitOutcome, resolve_commit};
use crate::values::{StorageValue, validate_node};
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
fn value_node_validation_is_bounded_and_base64_chunks_cannot_hide_padding() {
    assert!(
        validate_node(&json!({"type":"byte_chunk","depth":1,"value":{"data":"AP8=","last":true}}))
            .is_ok()
    );
    assert!(
        validate_node(&json!({"type":"byte_chunk","depth":1,"value":{"data":"AP8=","last":false}}))
            .is_err()
    );
    assert_eq!(
        validate_node(&json!({"type":"json","depth":33,"value":null}))
            .unwrap_err()
            .code,
        "STORAGE_PROCESS_LIMIT"
    );
    assert!(
        validate_node(
            &json!({"type":"document","depth":0,"value":{"id":"x","revision":true,
        "value_kind":"mapping"}})
        )
        .is_err()
    );
}
