use crate::{Coordinator, Error, PROTOCOL};
use serde_json::{Value, json};

fn plugin(id: &str, provides: &[&str], requires: &[&str]) -> Value {
    json!({"id":id,"version":"1","provides":provides,"requires":requires})
}

fn request(runtime: &mut Coordinator, command: &str, fields: Value) -> Result<Value, Error> {
    let mut value = fields.as_object().unwrap().clone();
    value.insert("command".into(), json!(format!("runtime_{command}")));
    value.insert("protocol".into(), json!(PROTOCOL));
    runtime.request(Value::Object(value))
}

fn create(runtime: &mut Coordinator, plugins: Vec<Value>, parent: Option<u64>) -> u64 {
    request(
        runtime,
        "create",
        json!({"plugins":plugins,"parent":parent}),
    )
    .unwrap()["host"]
        .as_u64()
        .unwrap()
}

fn poll(runtime: &mut Coordinator, host: u64) -> Vec<Value> {
    request(runtime, "poll", json!({"host":host})).unwrap()["actions"]
        .as_array()
        .unwrap()
        .clone()
}

fn complete(runtime: &mut Coordinator, action: &Value, status: &str, services: &[&str]) -> Value {
    request(runtime, "complete", json!({"host":action["host"],"operation":action["operation"],"status":status,"services":services})).unwrap()
}

fn finish_cleanup(runtime: &mut Coordinator, host: u64) -> Vec<(String, u64)> {
    let mut cleanup = Vec::new();
    loop {
        let actions = poll(runtime, host);
        if actions.is_empty() {
            break;
        }
        for action in actions {
            if action["kind"] == "cleanup" {
                cleanup.push((
                    action["plugin"].as_str().unwrap().into(),
                    action["resource"].as_u64().unwrap(),
                ));
                complete(runtime, &action, "ok", &[]);
            }
        }
    }
    cleanup
}

fn inspect(runtime: &mut Coordinator, host: u64) -> Value {
    request(runtime, "inspect", json!({"host":host})).unwrap()
}

#[test]
fn strict_validation_rejects_invalid_graphs_without_creating_instances() {
    let mut runtime = Coordinator::default();
    for (plugins, code) in [
        (vec![plugin("a", &["x"], &["missing"])], "missing_service"),
        (
            vec![plugin("a", &["x"], &["y"]), plugin("b", &["y"], &["x"])],
            "dependency_cycle",
        ),
        (
            vec![plugin("a", &["x"], &[]), plugin("b", &["x"], &[])],
            "duplicate_service",
        ),
        (
            vec![plugin("a", &["x"], &[]), plugin("a", &["y"], &[])],
            "duplicate_plugin",
        ),
    ] {
        assert_eq!(
            request(&mut runtime, "validate", json!({"plugins":plugins}))
                .unwrap_err()
                .code,
            code
        );
    }
    assert_eq!(create(&mut runtime, vec![], None), 1);
}

#[test]
fn init_actions_publish_only_declared_completed_services_and_cleanup_reverses_dependencies() {
    let mut runtime = Coordinator::default();
    let host = create(
        &mut runtime,
        vec![
            plugin("consumer", &["run"], &["model"]),
            plugin("model", &["model"], &[]),
        ],
        None,
    );
    let actions = poll(&mut runtime, host);
    assert_eq!(actions.len(), 1);
    assert_eq!(actions[0]["plugin"], "model");
    assert_eq!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":host,"service":"model"})
        )
        .unwrap_err()
        .code,
        "service_inactive"
    );
    complete(&mut runtime, &actions[0], "ok", &["model"]);
    let actions = poll(&mut runtime, host);
    assert_eq!(actions[0]["bindings"]["model"]["plugin"], "model");
    complete(&mut runtime, &actions[0], "ok", &["run"]);
    assert_eq!(inspect(&mut runtime, host)["state"], "ready");
    request(&mut runtime, "drain", json!({"host":host})).unwrap();
    assert_eq!(
        finish_cleanup(&mut runtime, host),
        vec![("consumer".into(), 0), ("model".into(), 0)]
    );
    assert_eq!(inspect(&mut runtime, host)["state"], "disposed");
}

#[test]
fn partial_initialization_failure_owns_resources_and_retry_never_repeats_successful_cleanup() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let action = poll(&mut runtime, host).remove(0);
    let mut resources = Vec::new();
    for _ in 0..2 {
        resources.push(
            request(
                &mut runtime,
                "resource",
                json!({"host":host,"operation":action["operation"],"kind":"connection"}),
            )
            .unwrap()["resource"]
                .as_u64()
                .unwrap(),
        );
    }
    complete(&mut runtime, &action, "error", &[]);
    let action = poll(&mut runtime, host).remove(0);
    assert_eq!(action["resource"], resources[1]);
    complete(&mut runtime, &action, "ok", &[]);
    let action = poll(&mut runtime, host).remove(0);
    assert_eq!(action["resource"], resources[0]);
    complete(&mut runtime, &action, "error", &[]);
    assert_eq!(inspect(&mut runtime, host)["state"], "cleanup_failed");
    assert!(poll(&mut runtime, host).is_empty());
    request(&mut runtime, "retry", json!({"host":host})).unwrap();
    assert_eq!(
        finish_cleanup(&mut runtime, host),
        vec![("p".into(), resources[0]), ("p".into(), 0)]
    );
    assert_eq!(inspect(&mut runtime, host)["state"], "failed");
    assert!(poll(&mut runtime, host).is_empty());
}

#[test]
fn completion_replay_is_idempotent_and_conflicts_do_not_rewrite_accepted_outcome() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let action = poll(&mut runtime, host).remove(0);
    assert_eq!(
        complete(&mut runtime, &action, "ok", &["service"])["duplicate"],
        false
    );
    assert_eq!(
        complete(&mut runtime, &action, "ok", &["service"])["duplicate"],
        true
    );
    assert_eq!(
        request(
            &mut runtime,
            "complete",
            json!({"host":host,"operation":action["operation"],"status":"error"})
        )
        .unwrap_err()
        .code,
        "conflicting_completion"
    );
    assert_eq!(inspect(&mut runtime, host)["state"], "ready");
    assert_eq!(
        inspect(&mut runtime, host)["diagnostics"][0]["code"],
        "conflicting_completion"
    );
}

#[test]
fn late_init_after_replacement_is_never_published_and_its_resources_are_cleaned() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let action = poll(&mut runtime, host).remove(0);
    request(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"p","config":{"revision":2}}),
    )
    .unwrap();
    let resource = request(
        &mut runtime,
        "resource",
        json!({"host":host,"operation":action["operation"],"kind":"late"}),
    )
    .unwrap()["resource"]
        .as_u64()
        .unwrap();
    complete(&mut runtime, &action, "ok", &["service"]);
    assert_eq!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":host,"service":"service"})
        )
        .unwrap_err()
        .code,
        "service_inactive"
    );
    let actions = poll(&mut runtime, host);
    assert!(actions.iter().any(|action| action["kind"] == "cancel"));
    for action in actions.iter().filter(|action| action["kind"] == "cleanup") {
        assert_eq!(action["resource"], resource);
        complete(&mut runtime, action, "ok", &[]);
    }
    let action = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &action, "ok", &[]);
    let successor = poll(&mut runtime, host).remove(0);
    assert_eq!(successor["generation"], 2);
    assert_eq!(successor["config"]["revision"], 2);
    complete(&mut runtime, &successor, "ok", &["service"]);
    assert_eq!(inspect(&mut runtime, host)["state"], "ready");
}

#[test]
fn cancelled_expired_invocation_pins_resources_until_real_completion() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let action = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &action, "ok", &["service"]);
    let binding = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"service"}),
    )
    .unwrap();
    let invoke = request(
        &mut runtime,
        "invoke",
        json!({"host":host,"registration":binding["registration"],"method":"work","deadline":10}),
    )
    .unwrap();
    request(&mut runtime, "tick", json!({"host":host,"now":10})).unwrap();
    request(&mut runtime, "drain", json!({"host":host})).unwrap();
    assert_eq!(inspect(&mut runtime, host)["leases"], 1);
    assert!(
        poll(&mut runtime, host)
            .iter()
            .all(|action| action["kind"] == "cancel")
    );
    assert_eq!(
        request(
            &mut runtime,
            "invoke",
            json!({"host":host,"registration":binding["registration"],"method":"work"})
        )
        .unwrap_err()
        .code,
        "scope_unavailable"
    );
    complete(&mut runtime, &invoke, "ok", &[]);
    assert_eq!(finish_cleanup(&mut runtime, host), vec![("p".into(), 0)]);
    assert_eq!(inspect(&mut runtime, host)["state"], "disposed");
}

#[test]
fn pending_dependencies_activate_and_consumer_cleanup_pins_replaced_provider() {
    let mut runtime = Coordinator::default();
    let host = create(
        &mut runtime,
        vec![plugin("consumer", &["run"], &["model"])],
        None,
    );
    assert_eq!(inspect(&mut runtime, host)["state"], "pending");
    assert!(poll(&mut runtime, host).is_empty());
    request(&mut runtime, "apply", json!({"host":host,"plugins":[plugin("consumer", &["run"], &["model"]),plugin("model", &["model"], &[])]})).unwrap();
    let action = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &action, "ok", &["model"]);
    let action = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &action, "ok", &["run"]);
    let old = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"model"}),
    )
    .unwrap();
    request(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"model","config":{"revision":2}}),
    )
    .unwrap();
    let actions = poll(&mut runtime, host);
    let cleanup = actions
        .iter()
        .find(|action| action["kind"] == "cleanup")
        .unwrap();
    assert_eq!(cleanup["plugin"], "consumer");
    let binding = request(&mut runtime, "resolve", json!({"host":host,"service":"model","consumer":cleanup["instance"],"consumer_generation":cleanup["generation"],"cleanup":cleanup["operation"]})).unwrap();
    assert_eq!(binding["registration"], old["registration"]);
    complete(&mut runtime, cleanup, "error", &[]);
    assert!(
        poll(&mut runtime, host)
            .iter()
            .all(|action| action["kind"] != "cleanup")
    );
    request(
        &mut runtime,
        "retry",
        json!({"host":host,"plugin":"consumer"}),
    )
    .unwrap();
    let cleanup = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &cleanup, "ok", &[]);
    let cleanup = poll(&mut runtime, host).remove(0);
    assert_eq!(cleanup["plugin"], "model");
    complete(&mut runtime, &cleanup, "ok", &[]);
    let action = poll(&mut runtime, host).remove(0);
    assert_eq!(action["plugin"], "model");
    complete(&mut runtime, &action, "ok", &["model"]);
    let action = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &action, "ok", &["run"]);
    let new = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"model"}),
    )
    .unwrap();
    assert_ne!(new["registration"], old["registration"]);
    assert_eq!(
        request(
            &mut runtime,
            "invoke",
            json!({"host":host,"registration":old["registration"],"method":"work"})
        )
        .unwrap_err()
        .code,
        "stale_binding"
    );
}

#[test]
fn scope_override_visibility_parent_ownership_and_listener_cleanup_are_explicit() {
    let mut runtime = Coordinator::default();
    let root = create(
        &mut runtime,
        vec![plugin("parent", &["service"], &[])],
        None,
    );
    let action = poll(&mut runtime, root).remove(0);
    request(
        &mut runtime,
        "listener",
        json!({"host":root,"operation":action["operation"],"event":"event"}),
    )
    .unwrap();
    complete(&mut runtime, &action, "ok", &["service"]);
    let old = request(
        &mut runtime,
        "resolve",
        json!({"host":root,"service":"service"}),
    )
    .unwrap();
    let child = create(&mut runtime, vec![], Some(root));
    assert_eq!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":child,"service":"service"})
        )
        .unwrap()["registration"],
        old["registration"]
    );
    let mut replacement = plugin("child", &["service"], &[]);
    replacement["overrides"] = json!(["service"]);
    let override_host = create(&mut runtime, vec![replacement], Some(root));
    assert_eq!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":override_host,"service":"service"})
        )
        .unwrap_err()
        .code,
        "service_inactive"
    );
    let action = poll(&mut runtime, override_host).remove(0);
    complete(&mut runtime, &action, "ok", &["service"]);
    assert_eq!(
        request(
            &mut runtime,
            "acquire",
            json!({"host":override_host,"registration":old["registration"]})
        )
        .unwrap_err()
        .code,
        "stale_binding"
    );
    request(&mut runtime, "drain", json!({"host":child})).unwrap();
    assert_eq!(
        inspect(&mut runtime, root)["children"],
        json!([override_host])
    );
    assert_eq!(
        request(
            &mut runtime,
            "listeners",
            json!({"host":root,"event":"event"})
        )
        .unwrap()["listeners"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    request(&mut runtime, "drain", json!({"host":root})).unwrap();
    finish_cleanup(&mut runtime, root);
    assert_eq!(inspect(&mut runtime, root)["state"], "disposed");
}

#[test]
fn full_catalog_and_nested_scope_admission_preserve_existing_leases() {
    let mut runtime = Coordinator::default();
    let root = create(
        &mut runtime,
        (0..256)
            .map(|i| plugin(&format!("plugin-{i}"), &[&format!("service-{i}")], &[]))
            .collect(),
        None,
    );
    loop {
        let actions = poll(&mut runtime, root);
        if actions.is_empty() {
            break;
        }
        for action in actions {
            let service = format!(
                "service-{}",
                action["plugin"]
                    .as_str()
                    .unwrap()
                    .strip_prefix("plugin-")
                    .unwrap()
            );
            complete(&mut runtime, &action, "ok", &[&service]);
        }
    }
    let mut child = root;
    for _ in 0..32 {
        child = create(&mut runtime, vec![], Some(child));
    }
    let lease = request(&mut runtime, "acquire", json!({"host":child})).unwrap()["lease"].clone();
    request(&mut runtime, "drain", json!({"host":root})).unwrap();
    assert_eq!(
        request(&mut runtime, "acquire", json!({"host":child}))
            .unwrap_err()
            .code,
        "scope_unavailable"
    );
    let nested = request(
        &mut runtime,
        "acquire",
        json!({"host":child,"parent_lease":lease}),
    )
    .unwrap()["lease"]
        .clone();
    request(
        &mut runtime,
        "release",
        json!({"host":child,"lease":nested}),
    )
    .unwrap();
    request(&mut runtime, "release", json!({"host":child,"lease":lease})).unwrap();
    finish_cleanup(&mut runtime, root);
    assert_eq!(inspect(&mut runtime, root)["state"], "disposed");
}

#[test]
fn invalid_apply_and_unknown_protocol_fields_leave_live_registration_unchanged() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let action = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &action, "ok", &["service"]);
    let before = inspect(&mut runtime, host);
    assert_eq!(
        request(
            &mut runtime,
            "apply",
            json!({"host":host,"plugins":[plugin("a", &["x"], &["y"]),plugin("b", &["y"], &["x"])]})
        )
        .unwrap_err()
        .code,
        "dependency_cycle"
    );
    assert_eq!(before, inspect(&mut runtime, host));
    assert!(request(&mut runtime, "inspect", json!({"host":host,"typo":true})).is_err());
    assert!(
        runtime
            .request(json!({"command":"runtime_inspect","host":host,"protocol":"plugin-runtime/2"}))
            .is_err()
    );
}

#[test]
fn native_async_task_reports_cleanup_only_after_cancellation_has_finished() {
    let executor = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    executor.block_on(async {
        let mut runtime = Coordinator::default();
        let host = create(
            &mut runtime,
            vec![plugin("native", &["service"], &[])],
            None,
        );
        let action = poll(&mut runtime, host).remove(0);
        let resource = request(
            &mut runtime,
            "resource",
            json!({"host":host,"operation":action["operation"],"kind":"task"}),
        )
        .unwrap()["resource"]
            .as_u64()
            .unwrap();
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (finish_tx, finish_rx) = tokio::sync::oneshot::channel();
        let task = crate::native::OwnedTask::spawn(resource, |mut stop| async move {
            started_tx.send(()).unwrap();
            stop.changed().await.unwrap();
            finish_rx.await.unwrap();
        });
        started_rx.await.unwrap();
        complete(&mut runtime, &action, "ok", &["service"]);
        request(&mut runtime, "drain", json!({"host":host})).unwrap();
        let cleanup = poll(&mut runtime, host)
            .into_iter()
            .find(|action| action["kind"] == "cleanup")
            .unwrap();
        assert_eq!(cleanup["resource"], task.resource());
        task.request_stop();
        tokio::task::yield_now().await;
        assert!(!task.is_finished());
        assert_eq!(inspect(&mut runtime, host)["state"], "cleaning");
        finish_tx.send(()).unwrap();
        task.finish().await.unwrap();
        complete(&mut runtime, &cleanup, "ok", &[]);
        finish_cleanup(&mut runtime, host);
        assert_eq!(inspect(&mut runtime, host)["state"], "disposed");
    });
}

#[test]
fn isolated_service_domains_and_nested_intercepts_preserve_definition_and_call_contexts() {
    let mut runtime = Coordinator::default();
    let root = request(&mut runtime, "create", json!({"plugins":[plugin("parent", &["service"], &[])],"intercept":{"service":{"format":{"language":"en","units":"m"}}}})).unwrap()["host"].as_u64().unwrap();
    let action = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &action, "ok", &["service"]);
    let child = request(&mut runtime, "create", json!({"parent":root,"plugins":[],"intercept":{"service":{"format":{"language":"zh"},"retry":false}}})).unwrap()["host"].as_u64().unwrap();
    let binding = request(
        &mut runtime,
        "resolve",
        json!({"host":child,"service":"service"}),
    )
    .unwrap();
    assert_eq!(binding["definition_scope"], root);
    assert_eq!(binding["call_scope"], child);
    assert_eq!(
        binding["intercept"],
        json!({"format":{"language":"zh","units":"m"},"retry":false})
    );
    let invoke = request(
        &mut runtime,
        "invoke",
        json!({"host":child,"registration":binding["registration"],"method":"work"}),
    )
    .unwrap();
    assert_eq!(invoke["binding"], binding);
    complete(&mut runtime, &invoke, "ok", &[]);
    let isolated = request(
        &mut runtime,
        "create",
        json!({"parent":root,"plugins":[],"isolated":["service"]}),
    )
    .unwrap()["host"]
        .as_u64()
        .unwrap();
    assert_eq!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":isolated,"service":"service"})
        )
        .unwrap_err()
        .code,
        "missing_service"
    );
    assert_eq!(
        request(
            &mut runtime,
            "acquire",
            json!({"host":isolated,"registration":binding["registration"]})
        )
        .unwrap_err()
        .code,
        "stale_binding"
    );
    let provider = request(&mut runtime, "create", json!({"parent":root,"plugins":[plugin("independent", &["service"], &[])],"isolated":["service"]})).unwrap()["host"].as_u64().unwrap();
    let action = poll(&mut runtime, provider).remove(0);
    complete(&mut runtime, &action, "ok", &["service"]);
    assert_ne!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":provider,"service":"service"})
        )
        .unwrap()["registration"],
        binding["registration"]
    );
    assert!(
        request(
            &mut runtime,
            "create",
            json!({"parent":root,"plugins":[],"isolated":["service","service"]})
        )
        .is_err()
    );
    assert!(
        request(
            &mut runtime,
            "create",
            json!({"parent":root,"plugins":[],"intercept":{"service":[]}})
        )
        .is_err()
    );
}

#[test]
fn switching_same_shape_implementation_restarts_only_that_dependency_subgraph() {
    let mut runtime = Coordinator::default();
    let mut original = plugin("provider", &["service"], &[]);
    original["implementation"] = json!("adapter-a");
    let independent = plugin("independent", &["other"], &[]);
    let host = create(
        &mut runtime,
        vec![original.clone(), independent.clone()],
        None,
    );
    for action in poll(&mut runtime, host) {
        let service = if action["plugin"] == "provider" {
            "service"
        } else {
            "other"
        };
        complete(&mut runtime, &action, "ok", &[service]);
    }
    let unchanged = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"other"}),
    )
    .unwrap();
    original["implementation"] = json!("adapter-b");
    request(
        &mut runtime,
        "apply",
        json!({"host":host,"plugins":[original,independent]}),
    )
    .unwrap();
    let actions = poll(&mut runtime, host);
    let cleanup = actions
        .iter()
        .find(|action| action["kind"] == "cleanup")
        .unwrap();
    assert_eq!(cleanup["implementation"], "adapter-a");
    complete(&mut runtime, cleanup, "ok", &[]);
    let action = poll(&mut runtime, host).remove(0);
    assert_eq!(action["implementation"], "adapter-b");
    complete(&mut runtime, &action, "ok", &["service"]);
    assert_eq!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":host,"service":"other"})
        )
        .unwrap(),
        unchanged
    );
}

#[test]
fn explicit_cancelled_init_cannot_activate_on_a_late_success() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let action = poll(&mut runtime, host).remove(0);
    request(
        &mut runtime,
        "cancel",
        json!({"host":host,"operation":action["operation"]}),
    )
    .unwrap();
    complete(&mut runtime, &action, "ok", &["service"]);
    finish_cleanup(&mut runtime, host);
    assert_eq!(inspect(&mut runtime, host)["state"], "failed");
    assert!(poll(&mut runtime, host).is_empty());
}

#[test]
fn active_internal_owner_can_lease_during_assembly_but_public_work_cannot_start() {
    let mut runtime = Coordinator::default();
    let host = create(
        &mut runtime,
        vec![
            plugin("consumer", &["run"], &["model"]),
            plugin("model", &["model"], &[]),
        ],
        None,
    );
    let provider = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &provider, "ok", &["model"]);
    let consumer = poll(&mut runtime, host).remove(0);
    assert_eq!(
        request(&mut runtime, "acquire", json!({"host":host}))
            .unwrap_err()
            .code,
        "scope_unavailable"
    );
    let lease = request(
        &mut runtime,
        "acquire",
        json!({"host":host,"instance":provider["instance"],"generation":provider["generation"]}),
    )
    .unwrap()["lease"]
        .clone();
    request(&mut runtime, "release", json!({"host":host,"lease":lease})).unwrap();
    complete(&mut runtime, &consumer, "ok", &["run"]);
    assert_eq!(inspect(&mut runtime, host)["state"], "ready");
}

#[test]
fn diagnostics_validate_runtime_identity_and_keep_parallel_failures_observable() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let action = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &action, "ok", &["service"]);
    let other = create(&mut runtime, vec![], None);
    assert_eq!(request(&mut runtime, "diagnostic", json!({"host":other,"code":"listener_error","message":"x","instance":action["instance"]})).unwrap_err().code, "diagnostic_identity");
    for n in 0..1030 {
        request(&mut runtime, "diagnostic", json!({"host":host,"code":"listener_error","message":n.to_string(),"instance":action["instance"]})).unwrap();
    }
    assert_eq!(
        inspect(&mut runtime, host)["diagnostics"]
            .as_array()
            .unwrap()
            .len(),
        1024
    );
    assert_eq!(
        inspect(&mut runtime, host)["diagnostics"][0]["message"],
        "6"
    );
    assert_eq!(inspect(&mut runtime, other)["diagnostics"], json!([]));
}

#[test]
fn explicit_collection_releases_completed_records_and_never_reuses_identity() {
    let mut runtime = Coordinator::default();
    let mut previous = 0;
    for _ in 0..32 {
        let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
        assert!(host > previous);
        previous = host;
        let action = poll(&mut runtime, host).remove(0);
        complete(&mut runtime, &action, "ok", &["service"]);
        assert_eq!(
            request(&mut runtime, "collect", json!({"host":host}))
                .unwrap_err()
                .code,
            "scope_busy"
        );
        request(&mut runtime, "drain", json!({"host":host})).unwrap();
        finish_cleanup(&mut runtime, host);
        let collected = request(&mut runtime, "collect", json!({"host":host})).unwrap();
        assert_eq!(collected["snapshot"]["state"], "disposed");
        for metric in [
            "scopes",
            "instances",
            "operations",
            "pending_operations",
            "resources",
            "listeners",
            "leases",
            "queued_actions",
        ] {
            assert_eq!(collected["remaining"][metric], 0, "{metric}");
        }
        assert_eq!(
            request(&mut runtime, "inspect", json!({"host":host}))
                .unwrap_err()
                .code,
            "unknown_host"
        );
    }
}

#[test]
fn inherited_listener_borrows_admitted_child_lease_without_exposing_hidden_service() {
    let mut runtime = Coordinator::default();
    let root = create(
        &mut runtime,
        vec![plugin("parent", &["service"], &[])],
        None,
    );
    let init = poll(&mut runtime, root).remove(0);
    request(
        &mut runtime,
        "listener",
        json!({"host":root,"operation":init["operation"],"event":"event"}),
    )
    .unwrap();
    complete(&mut runtime, &init, "ok", &["service"]);
    let binding = request(
        &mut runtime,
        "resolve",
        json!({"host":root,"service":"service"}),
    )
    .unwrap();
    let child = request(
        &mut runtime,
        "create",
        json!({"parent":root,"plugins":[],"isolated":["service"]}),
    )
    .unwrap()["host"]
        .as_u64()
        .unwrap();
    let lease = request(&mut runtime, "acquire", json!({"host":child})).unwrap()["lease"].clone();
    assert_eq!(
        request(
            &mut runtime,
            "acquire",
            json!({"host":child,"registration":binding["registration"],"parent_lease":lease})
        )
        .unwrap_err()
        .code,
        "stale_binding"
    );
    request(&mut runtime, "drain", json!({"host":root})).unwrap();
    let observer = request(
        &mut runtime,
        "acquire",
        json!({"host":root,"instance":init["instance"],"generation":init["generation"],"parent_lease":lease}),
    )
    .unwrap()["lease"]
        .clone();
    assert!(
        poll(&mut runtime, root)
            .iter()
            .all(|action| action["kind"] != "cleanup")
    );
    request(&mut runtime, "release", json!({"host":child,"lease":lease})).unwrap();
    assert!(
        poll(&mut runtime, root)
            .iter()
            .all(|action| action["kind"] != "cleanup")
    );
    request(
        &mut runtime,
        "release",
        json!({"host":root,"lease":observer}),
    )
    .unwrap();
    finish_cleanup(&mut runtime, root);
    assert_eq!(inspect(&mut runtime, root)["state"], "disposed");
}

#[test]
fn admitted_child_scope_can_finish_nested_inherited_call_after_ancestor_drain() {
    let mut runtime = Coordinator::default();
    let root = create(
        &mut runtime,
        vec![plugin("parent", &["service"], &[])],
        None,
    );
    let init = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    let child = create(&mut runtime, vec![], Some(root));
    let binding = request(
        &mut runtime,
        "resolve",
        json!({"host":child,"service":"service"}),
    )
    .unwrap();
    let lease = request(&mut runtime, "acquire", json!({"host":child})).unwrap()["lease"].clone();
    request(&mut runtime, "drain", json!({"host":root})).unwrap();
    let invoke = request(&mut runtime, "invoke", json!({"host":child,"registration":binding["registration"],"method":"finish","parent_lease":lease})).unwrap();
    complete(&mut runtime, &invoke, "ok", &[]);
    request(&mut runtime, "release", json!({"host":child,"lease":lease})).unwrap();
    finish_cleanup(&mut runtime, root);
    assert_eq!(inspect(&mut runtime, root)["state"], "disposed");
}

#[test]
fn never_dispatched_init_is_cancelled_without_running_mount_and_update_uses_new_generation() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    request(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"p","config":{"new":true}}),
    )
    .unwrap();
    let actions = poll(&mut runtime, host);
    assert!(actions.iter().all(|action| action["kind"] != "init"));
    let cleanup = actions
        .iter()
        .find(|action| action["kind"] == "cleanup")
        .unwrap();
    assert_eq!(cleanup["resource"], 0);
    complete(&mut runtime, cleanup, "ok", &[]);
    let init = poll(&mut runtime, host).remove(0);
    assert_eq!(init["generation"], 2);
    assert_eq!(init["config"], json!({"new":true}));
    complete(&mut runtime, &init, "ok", &["service"]);
    let terminal = create(&mut runtime, vec![plugin("q", &[], &[])], None);
    request(&mut runtime, "drain", json!({"host":terminal})).unwrap();
    let actions = poll(&mut runtime, terminal);
    assert!(actions.iter().all(|action| action["kind"] != "init"));
}

#[test]
fn collecting_disposed_child_cannot_leave_parent_revision_references_or_invoke_records() {
    let mut runtime = Coordinator::default();
    let root = create(
        &mut runtime,
        vec![plugin("parent", &["service"], &[])],
        None,
    );
    let init = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    let child = create(
        &mut runtime,
        vec![plugin("consumer", &["run"], &["service"])],
        Some(root),
    );
    let init = poll(&mut runtime, child).remove(0);
    complete(&mut runtime, &init, "ok", &["run"]);
    let binding = request(
        &mut runtime,
        "resolve",
        json!({"host":child,"service":"service"}),
    )
    .unwrap();
    let invoke = request(
        &mut runtime,
        "invoke",
        json!({"host":child,"registration":binding["registration"],"method":"work"}),
    )
    .unwrap();
    complete(&mut runtime, &invoke, "ok", &[]);
    request(
        &mut runtime,
        "update",
        json!({"host":root,"plugin":"parent","config":{"revision":2}}),
    )
    .unwrap();
    request(&mut runtime, "drain", json!({"host":child})).unwrap();
    finish_cleanup(&mut runtime, child);
    request(&mut runtime, "collect", json!({"host":child})).unwrap();
    let actions = poll(&mut runtime, root);
    for action in actions.iter().filter(|action| action["kind"] == "cleanup") {
        complete(&mut runtime, action, "ok", &[]);
    }
    let init = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    assert_eq!(inspect(&mut runtime, root)["state"], "ready");
}

#[test]
fn previous_generation_owner_and_dependency_handles_cannot_rebind_after_restart() {
    let mut runtime = Coordinator::default();
    let host = create(
        &mut runtime,
        vec![
            plugin("provider", &["service"], &[]),
            plugin("consumer", &["run"], &["service"]),
        ],
        None,
    );
    let provider = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &provider, "ok", &["service"]);
    let consumer = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &consumer, "ok", &["run"]);
    request(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"provider","config":{"revision":2}}),
    )
    .unwrap();
    let mut successor = None;
    loop {
        let actions = poll(&mut runtime, host);
        if actions.is_empty() {
            break;
        }
        for action in actions {
            match action["kind"].as_str().unwrap() {
                "cleanup" => {
                    complete(&mut runtime, &action, "ok", &[]);
                }
                "init" => {
                    let service = if action["plugin"] == "provider" {
                        "service"
                    } else {
                        successor = Some(action.clone());
                        "run"
                    };
                    complete(&mut runtime, &action, "ok", &[service]);
                }
                _ => (),
            }
        }
    }
    assert!(successor.is_some());
    assert_eq!(
        request(
            &mut runtime,
            "acquire",
            json!({"host":host,"instance":consumer["instance"],"generation":consumer["generation"]})
        )
        .unwrap_err()
        .code,
        "stale_generation"
    );
    assert_eq!(request(&mut runtime, "resolve", json!({"host":host,"service":"service","consumer":consumer["instance"],"consumer_generation":consumer["generation"]})).unwrap_err().code, "stale_generation");
    let current = successor.unwrap();
    let lease = request(
        &mut runtime,
        "acquire",
        json!({"host":host,"instance":current["instance"],"generation":current["generation"]}),
    )
    .unwrap()["lease"]
        .clone();
    request(&mut runtime, "release", json!({"host":host,"lease":lease})).unwrap();
}

#[test]
fn acknowledged_terminal_operations_do_not_accumulate_in_a_long_lived_runtime() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let init = poll(&mut runtime, host).remove(0);
    assert_eq!(
        request(
            &mut runtime,
            "ack",
            json!({"host":host,"operations":[init["operation"]]})
        )
        .unwrap_err()
        .code,
        "operation_pending"
    );
    complete(&mut runtime, &init, "ok", &["service"]);
    request(
        &mut runtime,
        "ack",
        json!({"host":host,"operations":[init["operation"]]}),
    )
    .unwrap();
    let binding = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"service"}),
    )
    .unwrap();
    for _ in 0..10_000 {
        let invoke = request(
            &mut runtime,
            "invoke",
            json!({"host":host,"registration":binding["registration"],"method":"work"}),
        )
        .unwrap();
        complete(&mut runtime, &invoke, "ok", &[]);
        request(
            &mut runtime,
            "ack",
            json!({"host":host,"operations":[invoke["operation"]]}),
        )
        .unwrap();
        request(
            &mut runtime,
            "ack",
            json!({"host":host,"operations":[invoke["operation"]]}),
        )
        .unwrap();
    }
    let snapshot = inspect(&mut runtime, host);
    assert_eq!(snapshot["state"], "ready");
    assert_eq!(snapshot["counts"]["operations"], 0);
    assert_eq!(snapshot["counts"]["leases"], 0);
    assert_eq!(snapshot["counts"]["queued_actions"], 0);
    assert_eq!(snapshot["counts"]["instances"], 1);
    assert_eq!(
        request(
            &mut runtime,
            "complete",
            json!({"host":host,"operation":init["operation"],"status":"ok","services":["service"]})
        )
        .unwrap_err()
        .code,
        "unknown_operation"
    );
}

#[test]
fn serial_plugin_scope_pins_allow_first_and_nested_work_but_reject_parallel_sibling_calls() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let init = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    let parent = request(&mut runtime, "acquire", json!({"host":host})).unwrap()["lease"].clone();
    let first = request(&mut runtime, "acquire", json!({"host":host,"instance":init["instance"],"generation":init["generation"],"parent_lease":parent})).unwrap()["lease"].clone();
    assert_eq!(request(&mut runtime, "acquire", json!({"host":host,"instance":init["instance"],"generation":init["generation"],"parent_lease":parent})).unwrap_err().code, "plugin_busy");
    let nested = request(&mut runtime, "acquire", json!({"host":host,"instance":init["instance"],"generation":init["generation"],"parent_lease":first})).unwrap()["lease"].clone();
    request(&mut runtime, "release", json!({"host":host,"lease":nested})).unwrap();
    request(&mut runtime, "release", json!({"host":host,"lease":first})).unwrap();
    request(&mut runtime, "release", json!({"host":host,"lease":parent})).unwrap();
}

#[test]
fn invocation_and_late_draining_effects_are_owned_until_real_call_end_and_cleanup_retry() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    let init = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    let binding = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"service"}),
    )
    .unwrap();
    let invoke = request(
        &mut runtime,
        "invoke",
        json!({"host":host,"registration":binding["registration"],"method":"work"}),
    )
    .unwrap();
    let task = request(
        &mut runtime,
        "resource",
        json!({"host":host,"operation":invoke["operation"],"kind":"task"}),
    )
    .unwrap()["resource"]
        .as_u64()
        .unwrap();
    let listener = request(
        &mut runtime,
        "listener",
        json!({"host":host,"operation":invoke["operation"],"event":"dynamic.event"}),
    )
    .unwrap()["resource"]
        .as_u64()
        .unwrap();
    assert_eq!(
        request(
            &mut runtime,
            "listeners",
            json!({"host":host,"event":"dynamic.event"})
        )
        .unwrap()["listeners"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    request(&mut runtime, "drain", json!({"host":host})).unwrap();
    let late = request(
        &mut runtime,
        "resource",
        json!({"host":host,"operation":invoke["operation"],"kind":"late"}),
    )
    .unwrap()["resource"]
        .as_u64()
        .unwrap();
    assert!(
        poll(&mut runtime, host)
            .iter()
            .all(|action| action["kind"] != "cleanup")
    );
    assert_eq!(inspect(&mut runtime, host)["counts"]["resources"], 3);
    complete(&mut runtime, &invoke, "error", &[]);
    assert_eq!(
        request(
            &mut runtime,
            "resource",
            json!({"host":host,"operation":invoke["operation"],"kind":"invalid"})
        )
        .unwrap_err()
        .code,
        "registration_closed"
    );
    let cleanup = poll(&mut runtime, host).remove(0);
    assert_eq!(cleanup["resource"], late);
    complete(&mut runtime, &cleanup, "error", &[]);
    assert_eq!(inspect(&mut runtime, host)["state"], "cleanup_failed");
    request(&mut runtime, "retry", json!({"host":host})).unwrap();
    assert_eq!(
        finish_cleanup(&mut runtime, host),
        vec![
            ("p".into(), late),
            ("p".into(), listener),
            ("p".into(), task),
            ("p".into(), 0)
        ]
    );
    assert_eq!(inspect(&mut runtime, host)["state"], "disposed");
}

#[test]
fn native_timer_resource_is_cancelled_and_joined_without_waiting_for_its_deadline() {
    let executor = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    executor.block_on(async {
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (ended_tx, ended_rx) = tokio::sync::oneshot::channel();
        let timer = crate::native::OwnedTask::spawn(7, |mut stop| async move {
            let timer = tokio::time::sleep(std::time::Duration::from_secs(3600));
            let mut timer = std::pin::pin!(timer);
            let cancelled = stop.changed();
            let mut cancelled = std::pin::pin!(cancelled);
            started_tx.send(()).unwrap();
            let outcome = std::future::poll_fn(|context| {
                if cancelled.as_mut().poll(context).is_ready() {
                    return std::task::Poll::Ready("cancelled");
                }
                if timer.as_mut().poll(context).is_ready() {
                    return std::task::Poll::Ready("elapsed");
                }
                std::task::Poll::Pending
            })
            .await;
            ended_tx.send(outcome).unwrap();
        });
        started_rx.await.unwrap();
        assert!(!timer.is_finished());
        timer.finish().await.unwrap();
        assert_eq!(ended_rx.await.unwrap(), "cancelled");
    });
}

#[test]
fn same_root_remove_and_readd_churn_reclaims_retired_instances_after_acknowledgement() {
    let mut runtime = Coordinator::default();
    let manifest = plugin("p", &["service"], &[]);
    let host = create(&mut runtime, vec![manifest.clone()], None);
    let init = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    request(
        &mut runtime,
        "ack",
        json!({"host":host,"operations":[init["operation"]]}),
    )
    .unwrap();
    let mut previous = init["instance"].as_u64().unwrap();
    for _ in 0..1000 {
        request(&mut runtime, "apply", json!({"host":host,"plugins":[]})).unwrap();
        for action in poll(&mut runtime, host) {
            if action["kind"] == "cleanup" {
                complete(&mut runtime, &action, "ok", &[]);
                request(
                    &mut runtime,
                    "ack",
                    json!({"host":host,"operations":[action["operation"]]}),
                )
                .unwrap();
            }
        }
        request(
            &mut runtime,
            "apply",
            json!({"host":host,"plugins":[manifest]}),
        )
        .unwrap();
        let init = poll(&mut runtime, host).remove(0);
        assert!(init["instance"].as_u64().unwrap() > previous);
        previous = init["instance"].as_u64().unwrap();
        complete(&mut runtime, &init, "ok", &["service"]);
        request(
            &mut runtime,
            "ack",
            json!({"host":host,"operations":[init["operation"]]}),
        )
        .unwrap();
        let snapshot = inspect(&mut runtime, host);
        assert_eq!(snapshot["counts"]["instances"], 1);
        assert_eq!(snapshot["instances"].as_array().unwrap().len(), 1);
        assert_eq!(snapshot["counts"]["operations"], 0);
        assert_eq!(snapshot["counts"]["resources"], 0);
        assert_eq!(snapshot["counts"]["leases"], 0);
    }
}

#[test]
fn retired_instance_completion_history_remains_until_explicit_ack_after_replacement() {
    let mut runtime = Coordinator::default();
    let manifest = plugin("p", &["service"], &[]);
    let host = create(&mut runtime, vec![manifest.clone()], None);
    let init = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &init, "ok", &["service"]);
    request(&mut runtime, "apply", json!({"host":host,"plugins":[]})).unwrap();
    let cleanup = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &cleanup, "ok", &[]);
    request(
        &mut runtime,
        "apply",
        json!({"host":host,"plugins":[manifest]}),
    )
    .unwrap();
    assert_eq!(
        complete(&mut runtime, &init, "ok", &["service"])["duplicate"],
        true
    );
    assert_eq!(inspect(&mut runtime, host)["counts"]["instances"], 2);
    request(
        &mut runtime,
        "ack",
        json!({"host":host,"operations":[init["operation"],cleanup["operation"]]}),
    )
    .unwrap();
    assert_eq!(inspect(&mut runtime, host)["counts"]["instances"], 1);
}

#[test]
fn unclaimed_cancelled_activations_do_not_accumulate_unacknowledgeable_reports() {
    let mut runtime = Coordinator::default();
    let host = create(&mut runtime, vec![plugin("p", &["service"], &[])], None);
    for revision in 1..1001 {
        request(
            &mut runtime,
            "update",
            json!({"host":host,"plugin":"p","config":{"revision":revision}}),
        )
        .unwrap();
        let cleanup = poll(&mut runtime, host).remove(0);
        assert_eq!(cleanup["kind"], "cleanup");
        complete(&mut runtime, &cleanup, "ok", &[]);
        request(
            &mut runtime,
            "ack",
            json!({"host":host,"operations":[cleanup["operation"]]}),
        )
        .unwrap();
        let snapshot = inspect(&mut runtime, host);
        assert_eq!(snapshot["counts"]["operations"], 1);
        assert_eq!(snapshot["counts"]["pending_operations"], 1);
        assert_eq!(snapshot["counts"]["queued_actions"], 1);
        assert_eq!(snapshot["counts"]["instances"], 1);
    }
}

#[test]
fn same_priority_listener_order_uses_actual_registration_after_a_pending_activation() {
    let mut runtime = Coordinator::default();
    let root = create(&mut runtime, vec![], None);
    let child = create(
        &mut runtime,
        vec![
            plugin("late", &["late"], &["gate"]),
            plugin("early", &["early"], &[]),
        ],
        Some(root),
    );
    let early = poll(&mut runtime, child).remove(0);
    assert_eq!(early["plugin"], "early");
    request(
        &mut runtime,
        "listener",
        json!({"host":child,"operation":early["operation"],"event":"event"}),
    )
    .unwrap();
    complete(&mut runtime, &early, "ok", &["early"]);
    request(
        &mut runtime,
        "apply",
        json!({"host":root,"plugins":[plugin("gate", &["gate"], &[])]}),
    )
    .unwrap();
    let gate = poll(&mut runtime, root).remove(0);
    complete(&mut runtime, &gate, "ok", &["gate"]);
    let late = poll(&mut runtime, child).remove(0);
    assert_eq!(late["plugin"], "late");
    request(
        &mut runtime,
        "listener",
        json!({"host":child,"operation":late["operation"],"event":"event"}),
    )
    .unwrap();
    complete(&mut runtime, &late, "ok", &["late"]);
    let listeners = request(
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
            .map(|row| row["plugin"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["early", "late"]
    );
}

#[test]
fn changed_configuration_file_fingerprint_restarts_only_bound_dependents_at_the_same_path() {
    let mut runtime = Coordinator::default();
    let mut provider = plugin("provider", &["service"], &[]);
    provider["config"] = json!({"config_path":"same-config.json"});
    provider["configuration_inputs"] = json!({"file":"a".repeat(64)});
    let host = create(
        &mut runtime,
        vec![
            provider,
            plugin("consumer", &["run"], &["service"]),
            plugin("independent", &["other"], &[]),
        ],
        None,
    );
    for init in poll(&mut runtime, host) {
        let service = if init["plugin"] == "provider" {
            "service"
        } else {
            "other"
        };
        complete(&mut runtime, &init, "ok", &[service]);
    }
    let consumer = poll(&mut runtime, host).remove(0);
    complete(&mut runtime, &consumer, "ok", &["run"]);
    let original = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"service"}),
    )
    .unwrap();
    let independent = request(
        &mut runtime,
        "resolve",
        json!({"host":host,"service":"other"}),
    )
    .unwrap();
    let before = inspect(&mut runtime, host);
    request(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"provider","configuration_inputs":{"file":"a".repeat(64)}}),
    )
    .unwrap();
    assert_eq!(inspect(&mut runtime, host), before);
    assert!(poll(&mut runtime, host).is_empty());
    request(
        &mut runtime,
        "update",
        json!({"host":host,"plugin":"provider","configuration_inputs":{"file":"b".repeat(64)}}),
    )
    .unwrap();
    let mut activated = Vec::new();
    loop {
        let actions = poll(&mut runtime, host);
        if actions.is_empty() {
            break;
        }
        for action in actions {
            if action["kind"] == "cleanup" {
                complete(&mut runtime, &action, "ok", &[]);
            } else if action["kind"] == "init" {
                activated.push(action["plugin"].as_str().unwrap().to_owned());
                let service = if action["plugin"] == "provider" {
                    assert_eq!(action["config"]["config_path"], "same-config.json");
                    assert_eq!(
                        action["configuration_inputs"],
                        json!({"file":"b".repeat(64)})
                    );
                    "service"
                } else {
                    "run"
                };
                complete(&mut runtime, &action, "ok", &[service]);
            }
        }
    }
    assert_eq!(activated, vec!["provider", "consumer"]);
    assert_ne!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":host,"service":"service"})
        )
        .unwrap()["registration"],
        original["registration"]
    );
    assert_eq!(
        request(
            &mut runtime,
            "resolve",
            json!({"host":host,"service":"other"})
        )
        .unwrap(),
        independent
    );
    assert_eq!(inspect(&mut runtime, host)["state"], "ready");
    let before = inspect(&mut runtime, host);
    assert_eq!(
        request(
            &mut runtime,
            "update",
            json!({"host":host,"plugin":"provider","configuration_inputs":{"file":"not-a-sha256"}})
        )
        .unwrap_err()
        .code,
        "invalid_manifest"
    );
    assert_eq!(inspect(&mut runtime, host), before);
}
