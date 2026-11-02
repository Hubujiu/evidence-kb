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

#[test]
fn overlapping_updates_keep_earlier_descendant_work_in_revision_fence() {
    for use_apply in [false, true] {
        for terminal in ["complete", "remove", "collect"] {
            let mut runtime = Coordinator::default();
            let root = create(
                &mut runtime,
                vec![plugin("p", &["p"], &[]), plugin("q", &["q"], &[])],
                None,
            );
            for init in poll(&mut runtime, root) {
                complete(
                    &mut runtime,
                    &init,
                    "ok",
                    &[init["plugin"].as_str().unwrap()],
                );
            }
            let child = create(
                &mut runtime,
                vec![plugin("consumer", &["run"], &["p"])],
                Some(root),
            );
            let initial = poll(&mut runtime, child).remove(0);
            complete(&mut runtime, &initial, "ok", &["run"]);
            call(
                &mut runtime,
                "update",
                json!({"host":root,"plugin":"p","config":{"revision":2}}),
            )
            .unwrap();
            if use_apply {
                let mut p = plugin("p", &["p"], &[]);
                p["config"] = json!({"revision":2});
                let mut q = plugin("q", &["q"], &[]);
                q["config"] = json!({"revision":2});
                call(
                    &mut runtime,
                    "apply",
                    json!({"host":root,"plugins":[p.clone(),q.clone()]}),
                )
                .unwrap();
                let revision =
                call(&mut runtime, "inspect", json!({"host":root})).unwrap()["desired_revision"]
                    .clone();
                call(&mut runtime, "apply", json!({"host":root,"plugins":[p,q]})).unwrap();
                assert_eq!(
                    call(&mut runtime, "inspect", json!({"host":root})).unwrap()["desired_revision"],
                    revision
                );
            } else {
                call(
                    &mut runtime,
                    "update",
                    json!({"host":root,"plugin":"q","config":{"revision":2}}),
                )
                .unwrap();
            }
            let mut pending_child = None;
            loop {
                let actions = poll(&mut runtime, root);
                if actions.is_empty() {
                    break;
                }
                for action in actions {
                    match action["kind"].as_str().unwrap() {
                        "cleanup" => complete(&mut runtime, &action, "ok", &[]),
                        "init" if action["plugin"] == "consumer" => pending_child = Some(action),
                        "init" => complete(
                            &mut runtime,
                            &action,
                            "ok",
                            &[action["plugin"].as_str().unwrap()],
                        ),
                        "cancel" => (),
                        kind => panic!("unexpected action {kind}"),
                    }
                }
            }
            let pending_child = pending_child.unwrap();
            assert_eq!(
                call(&mut runtime, "inspect", json!({"host":child})).unwrap()["state"],
                "starting"
            );
            let before = call(&mut runtime, "inspect", json!({"host":root})).unwrap();
            assert_eq!(
                before["operation"]["status"], "pending",
                "root snapshot: {before}"
            );
            assert!(
                before["observed_revision"].as_u64().unwrap()
                    < before["desired_revision"].as_u64().unwrap()
            );
            assert!(
                before["operation"]["affected_instances"]
                    .as_array()
                    .unwrap()
                    .contains(&pending_child["instance"])
            );
            if terminal == "complete" {
                complete(&mut runtime, &pending_child, "ok", &["run"]);
            } else {
                if terminal == "collect" {
                    call(&mut runtime, "drain", json!({"host":child})).unwrap();
                } else {
                    call(
                        &mut runtime,
                        "update",
                        json!({"host":child,"plugin":"consumer","remove":true}),
                    )
                    .unwrap();
                }
                complete(&mut runtime, &pending_child, "ok", &["run"]);
                loop {
                    let actions = poll(&mut runtime, child);
                    if actions.is_empty() {
                        break;
                    }
                    for action in actions {
                        if action["kind"] == "cleanup" {
                            complete(&mut runtime, &action, "ok", &[]);
                        }
                    }
                }
                if terminal == "collect" {
                    call(&mut runtime, "collect", json!({"host":child})).unwrap();
                }
            }
            let after = call(&mut runtime, "inspect", json!({"host":root})).unwrap();
            assert_eq!(after["operation"]["status"], "complete");
            assert_eq!(after["observed_revision"], after["desired_revision"]);
            if terminal == "complete" {
                call(
                    &mut runtime,
                    "update",
                    json!({"host":child,"plugin":"consumer","config":{"independent":true}}),
                )
                .unwrap();
                if use_apply {
                    let mut p = plugin("p", &["p"], &[]);
                    p["config"] = json!({"revision":2});
                    let mut q = plugin("q", &["q"], &[]);
                    q["config"] = json!({"revision":3});
                    call(&mut runtime, "apply", json!({"host":root,"plugins":[p,q]})).unwrap();
                } else {
                    call(
                        &mut runtime,
                        "update",
                        json!({"host":root,"plugin":"q","config":{"revision":3}}),
                    )
                    .unwrap();
                }
                let mut independent = None;
                loop {
                    let actions = poll(&mut runtime, root);
                    if actions.is_empty() {
                        break;
                    }
                    for action in actions {
                        match action["kind"].as_str().unwrap() {
                            "cleanup" => complete(&mut runtime, &action, "ok", &[]),
                            "init" if action["plugin"] == "consumer" => independent = Some(action),
                            "init" => complete(
                                &mut runtime,
                                &action,
                                "ok",
                                &[action["plugin"].as_str().unwrap()],
                            ),
                            "cancel" => (),
                            kind => panic!("unexpected action {kind}"),
                        }
                    }
                }
                let independent = independent.unwrap();
                let latest = call(&mut runtime, "inspect", json!({"host":root})).unwrap();
                assert_eq!(latest["operation"]["status"], "complete");
                assert!(
                    !latest["operation"]["affected_instances"]
                        .as_array()
                        .unwrap()
                        .contains(&independent["instance"])
                );
                assert_eq!(
                    call(&mut runtime, "inspect", json!({"host":child})).unwrap()["state"],
                    "starting"
                );
                complete(&mut runtime, &independent, "ok", &["run"]);
            }
        }
    }
}

#[test]
fn retained_registration_keeps_definition_identity_after_dynamic_shadowing() {
    let mut runtime = Coordinator::default();
    let root = call(&mut runtime, "create", json!({"plugins":[plugin("root", &["service"], &[])],"intercept":{"service":{"root":"retained","nested":{"outer":true}}}})).unwrap()["host"].as_u64().unwrap();
    let init = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    let child = call(
        &mut runtime,
        "create",
        json!({"plugins":[],"parent":root,"intercept":{"service":{"nested":{"inner":true}}}}),
    )
    .unwrap()["host"]
        .as_u64()
        .unwrap();
    let original = call(
        &mut runtime,
        "resolve",
        json!({"host":child,"service":"service"}),
    )
    .unwrap();
    let pin = call(&mut runtime, "acquire", json!({"host":child})).unwrap()["lease"].clone();
    let mut replacement = plugin("shadow", &["service"], &[]);
    replacement["overrides"] = json!(["service"]);
    call(
        &mut runtime,
        "apply",
        json!({"host":child,"plugins":[replacement]}),
    )
    .unwrap();
    let shadow = poll(&mut runtime, child).remove(0);
    complete(&mut runtime, &shadow, "ok", &["service"]);
    let visible = call(
        &mut runtime,
        "resolve",
        json!({"host":child,"service":"service"}),
    )
    .unwrap();
    assert_ne!(visible["registration"], original["registration"]);
    assert_eq!(
        call(
            &mut runtime,
            "invoke",
            json!({"host":child,"registration":original["registration"],"method":"fresh"})
        )
        .unwrap_err()
        .code,
        "stale_binding"
    );
    let invocation = call(&mut runtime, "invoke", json!({"host":child,"registration":original["registration"],"method":"retained","parent_lease":pin})).unwrap();
    assert_eq!(invocation["instance"], original["instance"]);
    assert_eq!(
        invocation["binding"], original,
        "admitted old registration cannot take the new shadow's definition metadata"
    );
    complete(&mut runtime, &invocation, "ok", &[]);
    call(&mut runtime, "release", json!({"host":child,"lease":pin})).unwrap();
}

#[test]
fn retained_registration_during_pending_shadow_does_not_strand_an_operation() {
    let mut runtime = Coordinator::default();
    let root = create(&mut runtime, vec![plugin("root", &["service"], &[])], None);
    let init = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    let child = create(&mut runtime, vec![], Some(root));
    let binding = call(
        &mut runtime,
        "resolve",
        json!({"host":child,"service":"service"}),
    )
    .unwrap();
    let pin = call(&mut runtime, "acquire", json!({"host":child})).unwrap()["lease"].clone();
    let mut replacement = plugin("shadow", &["service"], &[]);
    replacement["overrides"] = json!(["service"]);
    call(
        &mut runtime,
        "apply",
        json!({"host":child,"plugins":[replacement]}),
    )
    .unwrap();
    let before = call(&mut runtime, "inspect", json!({"host":root})).unwrap()["counts"].clone();
    let result = call(
        &mut runtime,
        "invoke",
        json!({"host":child,"registration":binding["registration"],"method":"retained","parent_lease":pin}),
    );
    let after = call(&mut runtime, "inspect", json!({"host":root})).unwrap()["counts"].clone();
    assert!(
        result.is_ok(),
        "error={:?}; before={before}; after={after}",
        result.as_ref().err().map(|error| &error.code)
    );
    let invocation = result.unwrap();
    assert_eq!(invocation["binding"], binding);
    complete(&mut runtime, &invocation, "ok", &[]);
    call(&mut runtime, "release", json!({"host":child,"lease":pin})).unwrap();
    let shadow = poll(&mut runtime, child).remove(0);
    complete(&mut runtime, &shadow, "ok", &["service"]);
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["counts"]["pending_operations"],
        0
    );
}

#[test]
fn subtree_collection_follows_detached_parent_links_and_call_scope_ownership() {
    for collect_root in [false, true] {
        let mut runtime = Coordinator::default();
        let root = create(&mut runtime, vec![plugin("root", &["service"], &[])], None);
        let init = poll(&mut runtime, root).remove(0);
        complete(&mut runtime, &init, "ok", &["service"]);
        let child = create(
            &mut runtime,
            vec![plugin("child", &["child"], &[])],
            Some(root),
        );
        let init = poll(&mut runtime, child).remove(0);
        complete(&mut runtime, &init, "ok", &["child"]);
        let grandchild = create(
            &mut runtime,
            vec![plugin("grandchild", &["grandchild"], &[])],
            Some(child),
        );
        let init = poll(&mut runtime, grandchild).remove(0);
        complete(&mut runtime, &init, "ok", &["grandchild"]);
        let foreign = create(
            &mut runtime,
            vec![plugin("foreign", &["foreign"], &[])],
            None,
        );
        let init = poll(&mut runtime, foreign).remove(0);
        complete(&mut runtime, &init, "ok", &["foreign"]);
        let foreign_before = call(&mut runtime, "inspect", json!({"host":foreign})).unwrap();
        for host in [root, child, grandchild] {
            call(
                &mut runtime,
                "diagnostic",
                json!({"host":host,"code":"owned","message":host.to_string()}),
            )
            .unwrap();
        }
        let binding = call(
            &mut runtime,
            "resolve",
            json!({"host":grandchild,"service":"service"}),
        )
        .unwrap();
        let invocation = call(
            &mut runtime,
            "invoke",
            json!({"host":grandchild,"registration":binding["registration"],"method":"work"}),
        )
        .unwrap();
        assert_eq!(invocation["binding"]["definition_scope"], root);
        assert_eq!(invocation["host"], grandchild);
        let target = if collect_root { root } else { child };
        call(&mut runtime, "drain", json!({"host":target})).unwrap();
        assert_eq!(
            call(&mut runtime, "collect", json!({"host":target}))
                .unwrap_err()
                .code,
            "scope_busy"
        );
        complete(&mut runtime, &invocation, "ok", &[]);
        loop {
            let actions = poll(&mut runtime, root);
            if actions.is_empty() {
                break;
            }
            for action in actions {
                if action["kind"] == "cleanup" {
                    complete(&mut runtime, &action, "ok", &[]);
                }
            }
        }
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":child})).unwrap()["children"],
            json!([])
        );
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":grandchild})).unwrap()["state"],
            "disposed"
        );
        let collected = call(&mut runtime, "collect", json!({"host":target})).unwrap();
        assert_eq!(
            collected["collected"]["scopes"],
            if collect_root { 3 } else { 2 }
        );
        assert_eq!(
            collected["collected"]["instances"],
            if collect_root { 3 } else { 2 }
        );
        for removed in [child, grandchild] {
            assert_eq!(
                call(&mut runtime, "inspect", json!({"host":removed}))
                    .unwrap_err()
                    .code,
                "unknown_host"
            );
        }
        if !collect_root {
            let remaining = call(&mut runtime, "inspect", json!({"host":root})).unwrap();
            assert_eq!(remaining["state"], "ready");
            assert_eq!(remaining["counts"]["operations"], 1);
            assert_eq!(remaining["counts"]["scopes"], 1);
            assert_eq!(remaining["diagnostics"].as_array().unwrap().len(), 1);
            assert_eq!(remaining["diagnostics"][0]["host"], root);
            assert_eq!(
                call(
                    &mut runtime,
                    "cancel",
                    json!({"host":root,"operation":invocation["operation"]})
                )
                .unwrap_err()
                .code,
                "unknown_operation"
            );
        }
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":foreign})).unwrap(),
            foreign_before
        );
        let new_host = create(&mut runtime, vec![], None);
        assert!(new_host > foreign);
        assert_ne!(new_host, child);
        assert_ne!(new_host, grandchild);
    }
}

#[test]
fn exited_notice_uses_full_incarnation_identity_after_remove_and_readd() {
    for publish in [false, true] {
        let mut runtime = Coordinator::default();
        let manifest = plugin("process", &["service"], &[]);
        let host = create(&mut runtime, vec![manifest.clone()], None);
        let old = poll(&mut runtime, host).remove(0);
        complete(&mut runtime, &old, "ok", &["service"]);
        call(&mut runtime, "apply", json!({"host":host,"plugins":[]})).unwrap();
        for action in poll(&mut runtime, host) {
            if action["kind"] == "cleanup" {
                complete(&mut runtime, &action, "ok", &[]);
            }
        }
        call(
            &mut runtime,
            "apply",
            json!({"host":host,"plugins":[manifest]}),
        )
        .unwrap();
        let current = poll(&mut runtime, host).remove(0);
        assert_ne!(old["instance"], current["instance"]);
        assert_eq!(old["generation"], current["generation"]);
        if publish {
            complete(&mut runtime, &current, "ok", &["service"]);
        }
        let foreign = create(&mut runtime, vec![plugin("process", &["other"], &[])], None);
        let foreign_init = poll(&mut runtime, foreign).remove(0);
        complete(&mut runtime, &foreign_init, "ok", &["other"]);
        let before = call(&mut runtime, "inspect", json!({"host":host})).unwrap();
        let other = call(&mut runtime, "inspect", json!({"host":foreign})).unwrap();
        for notice in [
            json!({"host":host,"plugin":"process","instance":old["instance"],"generation":old["generation"]}),
            json!({"host":foreign,"plugin":"process","instance":current["instance"],"generation":current["generation"]}),
            json!({"host":host,"plugin":"other","instance":current["instance"],"generation":current["generation"]}),
            json!({"host":host,"plugin":"process","instance":foreign_init["instance"],"generation":foreign_init["generation"]}),
            json!({"host":host,"plugin":"process","instance":current["instance"],"generation":99}),
        ] {
            assert_eq!(
                call(&mut runtime, "instance_exited", notice).unwrap()["accepted"],
                false
            );
            assert_eq!(
                call(&mut runtime, "inspect", json!({"host":host})).unwrap(),
                before
            );
            assert_eq!(
                call(&mut runtime, "inspect", json!({"host":foreign})).unwrap(),
                other
            );
        }
        let notice = json!({"host":host,"plugin":"process","instance":current["instance"],"generation":current["generation"]});
        assert_eq!(
            call(&mut runtime, "instance_exited", notice.clone()).unwrap()["accepted"],
            true
        );
        assert_eq!(
            call(&mut runtime, "instance_exited", notice).unwrap()["accepted"],
            false
        );
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":host})).unwrap()["instances"][0]["desired"],
            "disabled"
        );
        if !publish {
            complete(&mut runtime, &current, "ok", &["service"]);
        }
        loop {
            let actions = poll(&mut runtime, host);
            if actions.is_empty() {
                break;
            }
            for action in actions {
                assert_ne!(action["kind"], "init");
                if action["kind"] == "cleanup" {
                    complete(&mut runtime, &action, "ok", &[]);
                }
            }
        }
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":host})).unwrap()["instances"][0]["desired"],
            "disabled"
        );
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":foreign})).unwrap(),
            other
        );
    }
}

#[test]
fn diagnostic_global_capacity_rejects_foreign_identity_without_eviction_and_collects_by_host() {
    let mut runtime = Coordinator::default();
    let root = create(&mut runtime, vec![plugin("root", &["service"], &[])], None);
    let root_init = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &root_init, "ok", &["service"]);
    let child = create(&mut runtime, vec![], Some(root));
    let foreign = create(&mut runtime, vec![plugin("foreign", &["other"], &[])], None);
    let foreign_init = poll(&mut runtime, foreign).remove(0);
    complete(&mut runtime, &foreign_init, "ok", &["other"]);
    for n in 0..1023 {
        call(&mut runtime, "diagnostic", json!({"host":root,"code":"event","message":n.to_string(),"operation":root_init["operation"]})).unwrap();
    }
    call(&mut runtime, "diagnostic", json!({"host":child,"code":"event","message":"child-one","operation":root_init["operation"]})).unwrap();
    let before =
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["diagnostics"].clone();
    assert_eq!(before.as_array().unwrap().len(), 1024);
    for invalid in [
        json!({"host":root,"code":"event","message":"foreign","operation":foreign_init["operation"]}),
        json!({"host":foreign,"code":"event","message":"foreign","operation":root_init["operation"]}),
        json!({"host":root,"code":"event","message":"unknown","instance":u64::MAX}),
    ] {
        assert_eq!(
            call(&mut runtime, "diagnostic", invalid).unwrap_err().code,
            "diagnostic_identity"
        );
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":root})).unwrap()["diagnostics"],
            before
        );
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":foreign})).unwrap()["diagnostics"],
            json!([])
        );
    }
    for invalid in [
        json!({"host":root,"code":"bad code","message":"x"}),
        json!({"host":root,"code":"event","message":"x".repeat(4097)}),
    ] {
        assert_eq!(
            call(&mut runtime, "diagnostic", invalid).unwrap_err().code,
            "invalid_diagnostic"
        );
        assert_eq!(
            call(&mut runtime, "inspect", json!({"host":root})).unwrap()["diagnostics"],
            before
        );
    }
    call(&mut runtime, "diagnostic", json!({"host":child,"code":"event","message":"child-two","instance":root_init["instance"],"operation":root_init["operation"]})).unwrap();
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["diagnostics"][0]["message"],
        "1"
    );
    call(&mut runtime, "diagnostic", json!({"host":foreign,"code":"event","message":"foreign-valid","operation":foreign_init["operation"]})).unwrap();
    let root_events =
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["diagnostics"].clone();
    assert_eq!(root_events.as_array().unwrap().len(), 1023);
    assert_eq!(root_events[0]["message"], "2");
    let foreign_events =
        call(&mut runtime, "inspect", json!({"host":foreign})).unwrap()["diagnostics"].clone();
    assert_eq!(foreign_events.as_array().unwrap().len(), 1);
    assert_eq!(foreign_events[0]["message"], "foreign-valid");
    call(&mut runtime, "drain", json!({"host":child})).unwrap();
    call(&mut runtime, "collect", json!({"host":child})).unwrap();
    let remaining =
        call(&mut runtime, "inspect", json!({"host":root})).unwrap()["diagnostics"].clone();
    assert_eq!(remaining.as_array().unwrap().len(), 1021);
    assert!(
        remaining
            .as_array()
            .unwrap()
            .iter()
            .all(|event| event["host"] == root)
    );
    assert_eq!(
        call(&mut runtime, "inspect", json!({"host":foreign})).unwrap()["diagnostics"],
        foreign_events
    );
}
