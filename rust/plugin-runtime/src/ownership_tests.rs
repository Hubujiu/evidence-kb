use crate::{Coordinator, Error, PROTOCOL};
use serde_json::{Value, json};

fn call(runtime: &mut Coordinator, command: &str, fields: Value) -> Result<Value, Error> {
    let mut fields = fields.as_object().unwrap().clone();
    fields.insert("protocol".into(), json!(PROTOCOL));
    fields.insert("command".into(), json!(format!("runtime_{command}")));
    runtime.request(Value::Object(fields))
}

fn plugin(id: &str, provides: &[&str], requires: &[&str]) -> Value {
    json!({"id":id,"version":"1","provides":provides,"requires":requires})
}

fn create(runtime: &mut Coordinator, plugins: Vec<Value>, parent: Option<u64>) -> u64 {
    call(
        runtime,
        "create",
        json!({"plugins":plugins,"parent":parent}),
    )
    .unwrap()["host"]
        .as_u64()
        .unwrap()
}

fn poll(runtime: &mut Coordinator, host: u64) -> Vec<Value> {
    call(runtime, "poll", json!({"host":host})).unwrap()["actions"]
        .as_array()
        .unwrap()
        .clone()
}

fn complete(runtime: &mut Coordinator, action: &Value, status: &str, services: &[&str]) {
    call(runtime, "complete", json!({"host":action["host"],"operation":action["operation"],"status":status,"services":services})).unwrap();
}

#[test]
fn cancelled_serial_chain_in_child_retains_both_providers_until_real_completion() {
    let mut runtime = Coordinator::default();
    let root = create(
        &mut runtime,
        vec![plugin("p", &["p"], &[]), plugin("q", &["q"], &[])],
        None,
    );
    for init in poll(&mut runtime, root) {
        let name = init["plugin"].as_str().unwrap();
        call(
            &mut runtime,
            "resource",
            json!({"host":root,"operation":init["operation"],"kind":"owned"}),
        )
        .unwrap();
        complete(&mut runtime, &init, "ok", &[name]);
    }
    let child = create(
        &mut runtime,
        vec![plugin("consumer", &["run"], &["p", "q"])],
        Some(root),
    );
    let init = poll(&mut runtime, child).remove(0);
    complete(&mut runtime, &init, "ok", &["run"]);
    let binding = call(&mut runtime, "resolve", json!({"host":child,"service":"p"})).unwrap();
    let pin = call(&mut runtime, "acquire", json!({"host":child})).unwrap()["lease"].clone();
    let first = call(&mut runtime, "invoke", json!({"host":child,"registration":binding["registration"],"method":"run","parent_lease":pin})).unwrap();
    call(
        &mut runtime,
        "cancel",
        json!({"host":child,"operation":first["operation"]}),
    )
    .unwrap();
    let nested = call(&mut runtime, "invoke", json!({"host":child,"registration":binding["registration"],"method":"finish","parent_lease":first["lease"]})).unwrap();
    assert_eq!(call(&mut runtime, "invoke", json!({"host":child,"registration":binding["registration"],"method":"sibling","parent_lease":pin})).unwrap_err().code, "plugin_busy");
    call(&mut runtime, "drain", json!({"host":root})).unwrap();
    assert!(
        poll(&mut runtime, root)
            .iter()
            .all(|action| action["kind"] == "cancel")
    );
    complete(&mut runtime, &nested, "ok", &[]);
    assert!(poll(&mut runtime, root).is_empty());
    complete(&mut runtime, &first, "ok", &[]);
    assert!(poll(&mut runtime, root).is_empty());
    call(&mut runtime, "release", json!({"host":child,"lease":pin})).unwrap();
    let mut order = Vec::new();
    loop {
        let actions = poll(&mut runtime, root);
        if actions.is_empty() {
            break;
        }
        for action in actions {
            assert_eq!(action["kind"], "cleanup");
            order.push(action["plugin"].as_str().unwrap().to_owned());
            complete(&mut runtime, &action, "ok", &[]);
        }
    }
    assert_eq!(order.first().unwrap(), "consumer");
    assert!(order.iter().any(|name| name == "p"));
    assert!(order.iter().any(|name| name == "q"));
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["state"],
        "disposed"
    );
}

#[test]
fn inherited_deadline_and_foreign_runtime_rejections_do_not_allocate_leases() {
    let mut runtime = Coordinator::default();
    let root = create(&mut runtime, vec![plugin("p", &["p"], &[])], None);
    let init = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &init, "ok", &["p"]);
    let child = create(&mut runtime, vec![], Some(root));
    let foreign = create(&mut runtime, vec![], None);
    let binding = call(&mut runtime, "resolve", json!({"host":child,"service":"p"})).unwrap();
    let pin =
        call(&mut runtime, "acquire", json!({"host":child,"deadline":2})).unwrap()["lease"].clone();
    let before =
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["counts"]["leases"].clone();
    assert_eq!(
        call(
            &mut runtime,
            "invoke",
            json!({"host":foreign,"registration":binding["registration"],"method":"run"})
        )
        .unwrap_err()
        .code,
        "lease_scope"
    );
    call(&mut runtime, "tick", json!({"host":child,"now":2})).unwrap();
    assert_eq!(call(&mut runtime, "invoke", json!({"host":child,"registration":binding["registration"],"parent_lease":pin,"deadline":10,"method":"run"})).unwrap_err().code, "deadline");
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["counts"]["leases"],
        before
    );
    call(&mut runtime, "release", json!({"host":child,"lease":pin})).unwrap();
}

#[test]
fn all_dependencies_gate_unclaimed_disable_and_reenable_dispatch() {
    let mut runtime = Coordinator::default();
    let host = create(
        &mut runtime,
        vec![
            plugin("p", &["p"], &[]),
            plugin("q", &["q"], &[]),
            plugin("consumer", &["run"], &["p", "q"]),
        ],
        None,
    );
    let providers = poll(&mut runtime, host);
    assert_eq!(providers.len(), 2);
    complete(
        &mut runtime,
        &providers[0],
        "ok",
        &[providers[0]["plugin"].as_str().unwrap()],
    );
    assert!(poll(&mut runtime, host).is_empty());
    complete(
        &mut runtime,
        &providers[1],
        "ok",
        &[providers[1]["plugin"].as_str().unwrap()],
    );
    call(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"consumer","enabled":false}),
    )
    .unwrap();
    call(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"consumer","enabled":true}),
    )
    .unwrap();
    let cleanup = poll(&mut runtime, host);
    assert_eq!(cleanup.len(), 1);
    assert_eq!(cleanup[0]["kind"], "cleanup");
    assert_eq!(cleanup[0]["resource"], 0);
    complete(&mut runtime, &cleanup[0], "ok", &[]);
    let init = poll(&mut runtime, host);
    assert_eq!(init.len(), 1);
    assert_eq!(init[0]["plugin"], "consumer");
    assert_eq!(init[0]["generation"], 2);
    complete(&mut runtime, &init[0], "ok", &["run"]);
    let binding = call(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"run"}),
    )
    .unwrap();
    assert_eq!(binding["generation"], 2);
}

#[test]
fn claimed_disabled_epoch_cannot_publish_after_reenable_or_overwrite_successor() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["p"], &[])], None);
    let old = poll(&mut runtime, host).remove(0);
    let resource = call(
        &mut runtime,
        "resource",
        json!({"host":host,"operation":old["operation"],"kind":"old_owned"}),
    )
    .unwrap()["resource"]
        .clone();
    call(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"p","enabled":false}),
    )
    .unwrap();
    call(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"p","enabled":true}),
    )
    .unwrap();
    assert!(
        poll(&mut runtime, host)
            .iter()
            .all(|action| action["kind"] == "cancel")
    );
    complete(&mut runtime, &old, "ok", &["p"]);
    assert_eq!(
        call(&mut runtime, "resolve", json!({"host":host,"service":"p"}))
            .unwrap_err()
            .code,
        "service_inactive"
    );
    let cleanup = poll(&mut runtime, host).remove(0);
    assert_eq!(cleanup["resource"], resource);
    complete(&mut runtime, &cleanup, "ok", &[]);
    let final_cleanup = poll(&mut runtime, host).remove(0);
    assert_eq!(final_cleanup["resource"], 0);
    complete(&mut runtime, &final_cleanup, "ok", &[]);
    let new = poll(&mut runtime, host).remove(0);
    assert_ne!(new["operation"], old["operation"]);
    assert_eq!(new["generation"], 2);
    complete(&mut runtime, &new, "ok", &["p"]);
    let before = call(&mut runtime, "resolve", json!({"host":host,"service":"p"})).unwrap();
    complete(&mut runtime, &old, "ok", &["p"]);
    assert_eq!(
        call(&mut runtime, "resolve", json!({"host":host,"service":"p"})).unwrap(),
        before
    );
    assert_eq!(
        call(
            &mut runtime,
            "acquire",
            json!({"host":host,"instance":old["instance"],"generation":old["generation"]})
        )
        .unwrap_err()
        .code,
        "stale_generation"
    );
}

#[test]
fn acknowledgement_validates_entire_batch_before_forgetting_any_completion() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["p"], &[])], None);
    let init = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &init, "ok", &["p"]);
    let binding = call(&mut runtime, "resolve", json!({"host":host,"service":"p"})).unwrap();
    let invoke = call(
        &mut runtime,
        "invoke",
        json!({"host":host,"registration":binding["registration"],"method":"work"}),
    )
    .unwrap();
    let other = create(&mut runtime, vec![plugin("q", &[], &[])], None);
    let foreign = poll(&mut runtime, other).remove(0);
    complete(&mut runtime, &foreign, "ok", &[]);
    assert_eq!(
        call(
            &mut runtime,
            "ack",
            json!({"host":host,"operations":[init["operation"],invoke["operation"]]})
        )
        .unwrap_err()
        .code,
        "operation_pending"
    );
    complete(&mut runtime, &init, "ok", &["p"]);
    assert_eq!(
        call(
            &mut runtime,
            "ack",
            json!({"host":host,"operations":[init["operation"],foreign["operation"]]})
        )
        .unwrap_err()
        .code,
        "operation_scope"
    );
    complete(&mut runtime, &init, "ok", &["p"]);
    complete(&mut runtime, &invoke, "ok", &[]);
    assert_eq!(
        call(
            &mut runtime,
            "complete",
            json!({"host":host,"operation":invoke["operation"],"status":"error","services":[]})
        )
        .unwrap_err()
        .code,
        "conflicting_completion"
    );
    complete(&mut runtime, &invoke, "ok", &[]);
    call(&mut runtime, "ack", json!({"host":host,"operations":[init["operation"],invoke["operation"],invoke["operation"]]})).unwrap();
    assert_eq!(
        call(
            &mut runtime,
            "complete",
            json!({"host":host,"operation":invoke["operation"],"status":"ok","services":[]})
        )
        .unwrap_err()
        .code,
        "unknown_operation"
    );
    complete(&mut runtime, &foreign, "ok", &[]);
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":host})).unwrap()["counts"]["operations"],
        0
    );
}

#[test]
fn listener_order_lifo_failure_retry_and_retirement_preserve_resource_identity() {
    let mut runtime = Coordinator::default();
    let root = create(&mut runtime, vec![plugin("root", &[], &[])], None);
    let root_init = poll(&mut runtime, root).remove(0);
    let root_listener = call(
        &mut runtime,
        "listener",
        json!({"host":root,"operation":root_init["operation"],"event":"event","priority":1}),
    )
    .unwrap()["resource"]
        .clone();
    complete(&mut runtime, &root_init, "ok", &[]);
    let child = create(&mut runtime, vec![plugin("p", &["p"], &[])], Some(root));
    let init = poll(&mut runtime, child).remove(0);
    let mut resources = Vec::new();
    for priority in [0, 1, 1] {
        resources.push(call(&mut runtime, "listener", json!({"host":child,"operation":init["operation"],"event":"event","priority":priority})).unwrap()["resource"].clone());
    }
    complete(&mut runtime, &init, "ok", &["p"]);
    let listeners = call(
        &mut runtime,
        "listeners",
        json!({"host":child,"event":"event"}),
    )
    .unwrap();
    assert_eq!(
        listeners["listeners"]
            .as_array()
            .unwrap()
            .iter()
            .map(|listener| listener["resource"].clone())
            .collect::<Vec<_>>(),
        vec![
            resources[0].clone(),
            root_listener.clone(),
            resources[1].clone(),
            resources[2].clone()
        ]
    );
    call(
        &mut runtime,
        "update",
        json!({"host":child,"plugin":"p","remove":true}),
    )
    .unwrap();
    let failed = poll(&mut runtime, child).remove(0);
    assert_eq!(failed["resource"], resources[2]);
    complete(&mut runtime, &failed, "error", &[]);
    let snapshot = call(&mut runtime, "inspect", json!({"host":child})).unwrap();
    assert_eq!(snapshot["state"], "cleanup_failed");
    assert_eq!(
        snapshot["instances"][0]["resources"]
            .as_array()
            .unwrap()
            .len(),
        3
    );
    assert!(poll(&mut runtime, child).is_empty());
    let listeners = call(
        &mut runtime,
        "listeners",
        json!({"host":child,"event":"event"}),
    )
    .unwrap();
    assert_eq!(
        listeners["listeners"]
            .as_array()
            .unwrap()
            .iter()
            .map(|listener| listener["resource"].clone())
            .collect::<Vec<_>>(),
        vec![root_listener]
    );
    call(&mut runtime, "retry", json!({"host":child,"plugin":"p"})).unwrap();
    let mut operations = vec![init["operation"].clone(), failed["operation"].clone()];
    let mut order = Vec::new();
    loop {
        let actions = poll(&mut runtime, child);
        if actions.is_empty() {
            break;
        }
        for action in actions {
            assert_eq!(action["kind"], "cleanup");
            assert_ne!(action["operation"], failed["operation"]);
            order.push(action["resource"].clone());
            operations.push(action["operation"].clone());
            complete(&mut runtime, &action, "ok", &[]);
        }
    }
    assert_eq!(
        order,
        vec![
            resources[2].clone(),
            resources[1].clone(),
            resources[0].clone(),
            json!(0)
        ]
    );
    call(
        &mut runtime,
        "apply",
        json!({"host":child,"plugins":[plugin("p", &["p"], &[])]}),
    )
    .unwrap();
    let successor = poll(&mut runtime, child).remove(0);
    assert_ne!(successor["instance"], init["instance"]);
    complete(&mut runtime, &successor, "ok", &["p"]);
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["counts"]["instances"],
        3
    );
    call(
        &mut runtime,
        "ack",
        json!({"host":child,"operations":operations}),
    )
    .unwrap();
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["counts"]["instances"],
        2
    );
    assert_eq!(
        call(&mut runtime, "resolve", json!({"host":child,"service":"p"})).unwrap()["instance"],
        successor["instance"]
    );
}
