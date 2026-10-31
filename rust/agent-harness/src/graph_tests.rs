use super::*;

fn config() -> GraphConfig {
    serde_json::from_value(json!({
        "start": "calculate",
        "nodes": {
            "calculate": {"kind": "tool", "routes": {"again": "calculate", "done": "publish"}},
            "publish": {"kind": "host", "routes": {"done": null}}
        }
    }))
    .unwrap()
}

fn create(registry: &mut GraphRegistry) -> String {
    registry.create(config()).unwrap()["graph_id"]
        .as_str()
        .unwrap()
        .to_owned()
}

#[test]
fn graph_routes_cycles_and_terminal_without_domain_data() {
    let mut registry = GraphRegistry::default();
    let id = create(&mut registry);
    assert_eq!(
        registry.next(&id).unwrap(),
        json!({"kind":"tool", "name":"calculate", "effect_id":1})
    );
    assert_eq!(
        registry.accept(&id, 1, "again").unwrap()["node"],
        "calculate"
    );
    assert_eq!(registry.next(&id).unwrap()["effect_id"], 2);
    assert_eq!(registry.accept(&id, 2, "done").unwrap()["node"], "publish");
    assert_eq!(registry.next(&id).unwrap()["kind"], "host");
    assert_eq!(
        registry.accept(&id, 3, "done").unwrap()["node"],
        Value::Null
    );
    assert_eq!(registry.next(&id).unwrap(), json!({"kind":"complete"}));
    assert_eq!(registry.close(&id), json!({"closed":true}));
    assert_eq!(registry.close(&id), json!({"closed":false}));
    assert_eq!(registry.next(&id).unwrap_err().code, "GRAPH_NOT_FOUND");
}

#[test]
fn stale_or_duplicate_result_closes_graph_without_replay() {
    let mut registry = GraphRegistry::default();
    let id = create(&mut registry);
    registry.next(&id).unwrap();
    registry.accept(&id, 1, "again").unwrap();
    registry.next(&id).unwrap();
    assert_eq!(
        registry.accept(&id, 1, "done").unwrap_err().code,
        "GRAPH_RESULT_MISMATCH"
    );
    assert_eq!(registry.next(&id).unwrap_err().code, "GRAPH_NOT_FOUND");
    let id = create(&mut registry);
    registry.next(&id).unwrap();
    registry.accept(&id, 1, "done").unwrap();
    assert_eq!(
        registry.accept(&id, 1, "done").unwrap_err().code,
        "GRAPH_RESULT_MISMATCH"
    );
}

#[test]
fn unknown_route_and_unsettled_next_close_graph() {
    let mut registry = GraphRegistry::default();
    let id = create(&mut registry);
    registry.next(&id).unwrap();
    assert_eq!(
        registry.accept(&id, 1, "invented").unwrap_err().code,
        "GRAPH_ROUTE_NOT_FOUND"
    );
    assert_eq!(registry.snapshot(&id).unwrap_err().code, "GRAPH_NOT_FOUND");
    let id = create(&mut registry);
    registry.next(&id).unwrap();
    assert_eq!(registry.next(&id).unwrap_err().code, "GRAPH_RESULT_PENDING");
    assert_eq!(registry.snapshot(&id).unwrap_err().code, "GRAPH_NOT_FOUND");
}

#[test]
fn invalid_graph_rejects_before_registration() {
    let mut registry = GraphRegistry::default();
    let mut invalid = config();
    invalid.start = "absent".into();
    assert_eq!(registry.create(invalid).unwrap_err().code, "INVALID_GRAPH");
    let mut invalid = config();
    invalid
        .nodes
        .get_mut("calculate")
        .unwrap()
        .routes
        .insert("bad".into(), Some("absent".into()));
    assert_eq!(registry.create(invalid).unwrap_err().code, "INVALID_GRAPH");
    let mut invalid = config();
    invalid.nodes.get_mut("calculate").unwrap().kind = "shell".into();
    assert_eq!(registry.create(invalid).unwrap_err().code, "INVALID_GRAPH");
    let mut invalid = config();
    invalid.nodes.get_mut("calculate").unwrap().routes.clear();
    assert_eq!(registry.create(invalid).unwrap_err().code, "INVALID_GRAPH");
    assert!(registry.graphs.is_empty());
}

#[test]
fn independent_graphs_keep_separate_cursors_and_nonreused_handles() {
    let mut registry = GraphRegistry::default();
    let first = create(&mut registry);
    let second = create(&mut registry);
    registry.next(&first).unwrap();
    registry.accept(&first, 1, "done").unwrap();
    assert_eq!(registry.snapshot(&second).unwrap()["node"], "calculate");
    assert_eq!(registry.snapshot(&second).unwrap()["operations_started"], 0);
    registry.close(&first);
    let third = create(&mut registry);
    assert_ne!(first, third);
    assert_eq!(
        registry.snapshot(&first).unwrap_err().code,
        "GRAPH_NOT_FOUND"
    );
}

#[test]
fn graph_protocol_rejects_extra_fields_and_bounds_configuration() {
    let reply = crate::call(
        br#"{"version":1,"command":"graph_create","start":"x","nodes":{},"payload":{}}"#,
    );
    assert_eq!(reply["error"]["code"], "INVALID_REQUEST");
    let mut registry = GraphRegistry::default();
    let mut too_many = config();
    for n in 0..256 {
        too_many.nodes.insert(
            format!("extra-{n}"),
            Node {
                kind: "host".into(),
                routes: BTreeMap::from([("done".into(), None)]),
            },
        );
    }
    assert_eq!(registry.create(too_many).unwrap_err().code, "INVALID_GRAPH");
    let mut invalid = config();
    invalid
        .nodes
        .get_mut("calculate")
        .unwrap()
        .routes
        .insert("x".repeat(129), None);
    assert_eq!(registry.create(invalid).unwrap_err().code, "INVALID_GRAPH");
}
