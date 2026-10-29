use crate::{HostRegistry, call};
use serde_json::{Value, json};

fn plugin(id: &str, provides: &[&str], requires: &[&str]) -> Value {
    json!({"id":id,"version":"1","provides":provides,"requires":requires})
}

fn create(registry: &mut HostRegistry, plugins: Vec<Value>, parent: Option<u64>) -> u64 {
    registry
        .request(json!({"command":"create","plugins":plugins,"parent":parent}))
        .unwrap()["host"]
        .as_u64()
        .unwrap()
}

fn ready(registry: &mut HostRegistry, host: u64) {
    let plan = registry
        .request(json!({"command":"inspect","host":host}))
        .unwrap();
    for id in plan["order"].as_array().unwrap() {
        registry
            .request(json!({"command":"activate","host":host,"plugin":id}))
            .unwrap();
    }
    registry
        .request(json!({"command":"ready","host":host}))
        .unwrap();
}

fn error(registry: &mut HostRegistry, request: Value, code: &str) {
    assert_eq!(registry.request(request).unwrap_err().code, code);
}

#[test]
fn dependency_order_governs_activation_and_reverse_disposal() {
    let mut registry = HostRegistry::default();
    let host = create(
        &mut registry,
        vec![
            plugin("consumer", &["run"], &["model"]),
            plugin("model", &["model"], &[]),
        ],
        None,
    );
    let state = registry
        .request(json!({"command":"inspect","host":host}))
        .unwrap();
    assert_eq!(state["order"], json!(["model", "consumer"]));
    error(
        &mut registry,
        json!({"command":"activate","host":host,"plugin":"consumer"}),
        "activation_order",
    );
    error(
        &mut registry,
        json!({"command":"resolve","host":host,"service":"model"}),
        "service_inactive",
    );
    ready(&mut registry, host);
    registry
        .request(json!({"command":"drain","host":host}))
        .unwrap();
    assert_eq!(
        registry
            .request(json!({"command":"dispose","host":host}))
            .unwrap()["order"],
        json!(["consumer", "model"])
    );
    registry
        .request(json!({"command":"finish_dispose","host":host}))
        .unwrap();
    error(
        &mut registry,
        json!({"command":"inspect","host":host}),
        "unknown_host",
    );
}

#[test]
fn rejected_graphs_do_not_consume_handles_or_create_children() {
    let mut registry = HostRegistry::default();
    for (plugins, code) in [
        (vec![plugin("a", &["x"], &["absent"])], "missing_service"),
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
        error(
            &mut registry,
            json!({"command":"create","plugins":plugins}),
            code,
        );
    }
    assert_eq!(create(&mut registry, vec![], None), 1);
}

#[test]
fn scope_lookup_and_override_are_explicit_and_isolated() {
    let mut registry = HostRegistry::default();
    let root = create(
        &mut registry,
        vec![plugin("model-a", &["model"], &[])],
        None,
    );
    ready(&mut registry, root);
    error(
        &mut registry,
        json!({"command":"create","parent":root,
        "plugins":[plugin("model-b", &["model"], &[])]}),
        "implicit_override",
    );
    let inherited = create(&mut registry, vec![], Some(root));
    ready(&mut registry, inherited);
    let mut replacement = plugin("model-b", &["model"], &[]);
    replacement["overrides"] = json!(["model"]);
    let overridden = create(&mut registry, vec![replacement], Some(root));
    ready(&mut registry, overridden);
    assert_eq!(
        registry
            .request(json!({"command":"resolve","host":inherited,"service":"model"}))
            .unwrap()["host"],
        root
    );
    assert_eq!(
        registry
            .request(json!({"command":"resolve","host":overridden,"service":"model"}))
            .unwrap()["host"],
        overridden
    );
    assert_eq!(
        registry
            .request(json!({"command":"resolve","host":root,"service":"model"}))
            .unwrap()["plugin"],
        "model-a"
    );
}

#[test]
fn draining_closes_admission_and_preserves_existing_lease_until_release() {
    let mut registry = HostRegistry::default();
    let host = create(&mut registry, vec![], None);
    ready(&mut registry, host);
    let lease = registry
        .request(json!({"command":"acquire","host":host}))
        .unwrap()["lease"]
        .clone();
    registry
        .request(json!({"command":"drain","host":host}))
        .unwrap();
    error(
        &mut registry,
        json!({"command":"acquire","host":host}),
        "scope_unavailable",
    );
    error(
        &mut registry,
        json!({"command":"dispose","host":host}),
        "scope_busy",
    );
    registry
        .request(json!({"command":"release","host":host,"lease":lease}))
        .unwrap();
    error(
        &mut registry,
        json!({"command":"release","host":host,"lease":lease}),
        "unknown_lease",
    );
    registry
        .request(json!({"command":"dispose","host":host}))
        .unwrap();
    error(
        &mut registry,
        json!({"command":"acquire","host":host}),
        "scope_unavailable",
    );
    error(
        &mut registry,
        json!({"command":"create","parent":host,"plugins":[]}),
        "scope_unavailable",
    );
    registry
        .request(json!({"command":"finish_dispose","host":host}))
        .unwrap();
}

#[test]
fn child_cleanup_pins_parent_until_explicit_completion() {
    let mut registry = HostRegistry::default();
    let root = create(&mut registry, vec![], None);
    ready(&mut registry, root);
    let child = create(&mut registry, vec![], Some(root));
    registry
        .request(json!({"command":"abort","host":child}))
        .unwrap();
    registry
        .request(json!({"command":"drain","host":root}))
        .unwrap();
    error(
        &mut registry,
        json!({"command":"dispose","host":root}),
        "scope_busy",
    );
    registry
        .request(json!({"command":"finish_dispose","host":child}))
        .unwrap();
    registry
        .request(json!({"command":"dispose","host":root}))
        .unwrap();
}

#[test]
fn failed_activation_returns_only_completed_effects_in_reverse_order() {
    let mut registry = HostRegistry::default();
    let host = create(
        &mut registry,
        vec![
            plugin("a", &["model"], &[]),
            plugin("b", &["run"], &["model"]),
        ],
        None,
    );
    registry
        .request(json!({"command":"activate","host":host,"plugin":"a"}))
        .unwrap();
    let cleanup = registry
        .request(json!({"command":"abort","host":host}))
        .unwrap();
    assert_eq!(cleanup["order"], json!(["a"]));
    assert_eq!(
        registry
            .request(json!({"command":"abort","host":host}))
            .unwrap(),
        cleanup
    );
    error(
        &mut registry,
        json!({"command":"ready","host":host}),
        "invalid_phase",
    );
    registry
        .request(json!({"command":"finish_dispose","host":host}))
        .unwrap();
    let successor = create(&mut registry, vec![], None);
    assert!(successor > host);
}

#[test]
fn parent_drain_stops_new_descendant_runs_without_revoking_existing_services() {
    let mut registry = HostRegistry::default();
    let root = create(&mut registry, vec![plugin("model", &["model"], &[])], None);
    ready(&mut registry, root);
    let child = create(&mut registry, vec![], Some(root));
    ready(&mut registry, child);
    registry
        .request(json!({"command":"drain","host":root}))
        .unwrap();
    error(
        &mut registry,
        json!({"command":"acquire","host":child}),
        "scope_unavailable",
    );
    assert!(
        registry
            .request(json!({"command":"resolve","host":child,"service":"model"}))
            .is_ok()
    );
}

#[test]
fn protocol_rejects_unknown_fields_versions_and_invalid_json() {
    for bytes in [
        br#"{"version":2,"command":"create","plugins":[]}"#.as_slice(),
        br#"{"version":1,"command":"create","plugins":[],"typo":true}"#.as_slice(),
        br#"{"version":1,"command":"create","plugins":[{"id":"x","version":"1","provides":[],"requires":[],"typo":true}]}"#.as_slice(),
        b"not json".as_slice(),
    ] {
        assert_eq!(call(bytes)["ok"], false);
    }
}

#[test]
fn full_catalog_resolves_all_services_without_reordering_manifests() {
    let mut registry = HostRegistry::default();
    let plugins: Vec<_> = (0..256)
        .rev()
        .map(|index| {
            plugin(
                &format!("plugin-{index}"),
                &[&format!("read-{index}"), &format!("write-{index}")],
                &[],
            )
        })
        .collect();
    let host = create(&mut registry, plugins.clone(), None);
    ready(&mut registry, host);
    for index in 0..256 {
        for operation in ["read", "write"] {
            assert_eq!(
                registry
                    .request(json!({"command":"resolve","host":host,
                        "service":format!("{operation}-{index}")}))
                    .unwrap(),
                json!({"host":host,"plugin":format!("plugin-{index}")})
            );
        }
    }
    let state = registry
        .request(json!({"command":"inspect","host":host}))
        .unwrap();
    let expected_ids: Vec<_> = (0..256)
        .rev()
        .map(|index| format!("plugin-{index}"))
        .collect();
    assert_eq!(state["order"], json!(expected_ids));
    assert_eq!(state["activated"], state["order"]);
    assert_eq!(state["plugins"][0]["id"], "plugin-255");
    assert_eq!(
        state["plugins"][255]["provides"],
        json!(["read-0", "write-0"])
    );
    error(
        &mut registry,
        json!({"command":"resolve","host":host,"service":"absent"}),
        "missing_service",
    );
}

#[test]
fn inactive_override_never_falls_back_to_parent_provider() {
    let mut registry = HostRegistry::default();
    let parent = create(&mut registry, vec![plugin("parent", &["model"], &[])], None);
    ready(&mut registry, parent);
    let mut replacement = plugin("replacement", &["model"], &[]);
    replacement["overrides"] = json!(["model"]);
    let child = create(&mut registry, vec![replacement], Some(parent));
    error(
        &mut registry,
        json!({"command":"resolve","host":child,"service":"model"}),
        "service_inactive",
    );
    registry
        .request(json!({"command":"activate","host":child,"plugin":"replacement"}))
        .unwrap();
    assert_eq!(
        registry
            .request(json!({"command":"resolve","host":child,"service":"model"}))
            .unwrap(),
        json!({"host":child,"plugin":"replacement"})
    );
}

#[test]
fn disposal_and_abort_preserve_cleanup_lookup_but_reject_new_leases() {
    let mut registry = HostRegistry::default();
    for abort in [false, true] {
        let host = create(&mut registry, vec![plugin("p", &["service"], &[])], None);
        registry
            .request(json!({"command":"activate","host":host,"plugin":"p"}))
            .unwrap();
        let command = if abort {
            "abort"
        } else {
            registry
                .request(json!({"command":"ready","host":host}))
                .unwrap();
            registry
                .request(json!({"command":"drain","host":host}))
                .unwrap();
            assert!(
                registry
                    .request(json!({"command":"resolve","host":host,
                "service":"service"}))
                    .is_ok()
            );
            "dispose"
        };
        registry
            .request(json!({"command":command,"host":host}))
            .unwrap();
        assert_eq!(
            registry
                .request(json!({"command":"resolve","host":host,"service":"service"}))
                .unwrap(),
            json!({"host":host,"plugin":"p"})
        );
        error(
            &mut registry,
            json!({"command":"acquire","host":host}),
            "scope_unavailable",
        );
        assert_eq!(
            registry
                .request(json!({"command":"inspect","host":host}))
                .unwrap()["state"],
            "disposing"
        );
    }
}

#[test]
fn invalid_lease_release_preserves_both_scopes_and_failed_admission_consumes_no_id() {
    let mut registry = HostRegistry::default();
    let first = create(&mut registry, vec![], None);
    let second = create(&mut registry, vec![], None);
    error(
        &mut registry,
        json!({"command":"acquire","host":first}),
        "scope_unavailable",
    );
    ready(&mut registry, first);
    ready(&mut registry, second);
    let first_lease = registry
        .request(json!({"command":"acquire","host":first}))
        .unwrap()["lease"]
        .clone();
    let second_lease = registry
        .request(json!({"command":"acquire","host":second}))
        .unwrap()["lease"]
        .clone();
    assert_eq!(first_lease, 3);
    assert_eq!(second_lease, 4);
    error(
        &mut registry,
        json!({"command":"release","host":second,"lease":first_lease}),
        "unknown_lease",
    );
    for host in [first, second] {
        registry
            .request(json!({"command":"drain","host":host}))
            .unwrap();
        error(
            &mut registry,
            json!({"command":"dispose","host":host}),
            "scope_busy",
        );
        assert_eq!(
            registry
                .request(json!({"command":"inspect","host":host}))
                .unwrap()["leases"],
            1
        );
        assert_eq!(
            registry
                .request(json!({"command":"inspect","host":host}))
                .unwrap()["state"],
            "draining"
        );
    }
    registry
        .request(json!({"command":"release","host":first,"lease":first_lease}))
        .unwrap();
    registry
        .request(json!({"command":"release","host":second,"lease":second_lease}))
        .unwrap();
    for host in [first, second] {
        registry
            .request(json!({"command":"dispose","host":host}))
            .unwrap();
    }
}

#[test]
fn deep_scope_lookup_and_ancestor_drain_preserve_existing_lease() {
    let mut registry = HostRegistry::default();
    let root = create(&mut registry, vec![plugin("root", &["service"], &[])], None);
    ready(&mut registry, root);
    let mut leaf = root;
    for _ in 0..256 {
        leaf = create(&mut registry, vec![], Some(leaf));
        ready(&mut registry, leaf);
    }
    let lease = registry
        .request(json!({"command":"acquire","host":leaf}))
        .unwrap()["lease"]
        .clone();
    registry
        .request(json!({"command":"drain","host":root}))
        .unwrap();
    error(
        &mut registry,
        json!({"command":"acquire","host":leaf}),
        "scope_unavailable",
    );
    assert_eq!(
        registry
            .request(json!({"command":"resolve","host":leaf,"service":"service"}))
            .unwrap(),
        json!({"host":root,"plugin":"root"})
    );
    registry
        .request(json!({"command":"release","host":leaf,"lease":lease}))
        .unwrap();
    assert_eq!(
        registry
            .request(json!({"command":"inspect","host":leaf}))
            .unwrap()["leases"],
        0
    );
}
