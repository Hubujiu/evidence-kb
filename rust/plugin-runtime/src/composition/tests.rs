use super::*;

fn fixture() -> (Application, Vec<Definition>) {
    let app = serde_json::from_value(json!({
        "version": 1,
        "plugins": {
            "api": {"implementation": "http", "version": "1", "config": {}},
            "database": {"implementation": "sqlite", "version": "1", "config": {}}
        },
        "bindings": {"api": {"records": {"plugin": "database", "export": "data"}}},
        "entrypoint": {"plugin": "api", "export": "server"}
    }))
    .unwrap();
    let catalog = serde_json::from_value(json!([
        {
            "implementation": "http", "version": "1", "command": ["python", "app.py"],
            "provides": {"server": {"id": "application.run", "major": 1}},
            "requires": {"records": {"contract": {
                "id": "storage.document", "major": 1, "capabilities": ["revision"]
            }}}
        },
        {
            "implementation": "sqlite", "version": "1", "command": ["node", "storage.mjs"],
            "provides": {"data": {"id": "storage.document", "major": 1,
                "capabilities": ["revision", "batch"]}}, "requires": {}
        }
    ]))
    .unwrap();
    (app, catalog)
}

#[test]
fn aliases_compile_into_existing_lifecycle_dependencies() {
    let (app, catalog) = fixture();
    let plan = compile(app, catalog).unwrap();
    assert_eq!(plan.order, ["database", "api"]);
    assert_eq!(plan.bindings["api"]["records"], "8.database:data");
    assert_eq!(plan.entrypoint, "3.api:server");
    assert_eq!(plan.commands["database"], ["node", "storage.mjs"]);
    let mut coordinator = crate::Coordinator::default();
    let result = coordinator.request(json!({"protocol": crate::PROTOCOL,
        "command": "runtime_validate", "plugins": plan.plugins}));
    assert!(result.is_ok(), "{result:?}");
}

#[test]
fn provider_can_change_without_changing_consumers() {
    let (mut app, mut catalog) = fixture();
    app.plugins.get_mut("database").unwrap().implementation = "postgres".into();
    catalog[1].implementation = "postgres".into();
    catalog[1].command = vec!["postgres-plugin".into()];
    let plan = compile(app, catalog).unwrap();
    assert_eq!(plan.bindings["api"]["records"], "8.database:data");
    assert_eq!(plan.order, ["database", "api"]);
}

#[test]
fn rejects_wrong_contract_version_and_missing_capability() {
    for variant in 0..3 {
        let (app, mut catalog) = fixture();
        let contract = catalog[1].provides.get_mut("data").unwrap();
        match variant {
            0 => contract.major = 2,
            1 => contract.id = "storage.kv".into(),
            _ => contract.capabilities.clear(),
        }
        assert_eq!(compile(app, catalog).unwrap_err().code, "contract_mismatch");
    }
}

#[test]
fn rejects_missing_or_undeclared_bindings_and_unknown_targets() {
    for variant in 0..4 {
        let (mut app, catalog) = fixture();
        match variant {
            0 => {
                app.bindings.clear();
            }
            1 => {
                app.bindings.get_mut("api").unwrap().insert(
                    "hidden".into(),
                    Target {
                        plugin: "database".into(),
                        export: "data".into(),
                    },
                );
            }
            2 => {
                app.bindings
                    .get_mut("api")
                    .unwrap()
                    .get_mut("records")
                    .unwrap()
                    .export = "missing".into();
            }
            _ => {
                app.bindings.insert("missing".into(), BTreeMap::new());
            }
        }
        assert_eq!(
            compile(app, catalog).unwrap_err().code,
            "invalid_composition"
        );
    }
}

#[test]
fn optional_dependency_can_be_absent_but_explicit_binding_must_match() {
    let (mut app, mut catalog) = fixture();
    catalog[0].requires.get_mut("records").unwrap().optional = true;
    app.bindings.clear();
    assert!(compile(app.clone(), catalog.clone()).is_ok());
    app.bindings.insert(
        "api".into(),
        BTreeMap::from([(
            "records".into(),
            Target {
                plugin: "api".into(),
                export: "server".into(),
            },
        )]),
    );
    assert_eq!(compile(app, catalog).unwrap_err().code, "contract_mismatch");
}

#[test]
fn rejects_cycles_and_non_application_entrypoints() {
    let (mut app, mut catalog) = fixture();
    catalog[1].requires.insert(
        "api".into(),
        Requirement {
            contract: Contract {
                id: "application.run".into(),
                major: 1,
                capabilities: BTreeSet::new(),
            },
            optional: false,
        },
    );
    app.bindings.insert(
        "database".into(),
        BTreeMap::from([("api".into(), app.entrypoint.clone())]),
    );
    assert_eq!(compile(app, catalog).unwrap_err().code, "dependency_cycle");
    let (mut app, catalog) = fixture();
    app.entrypoint = Target {
        plugin: "database".into(),
        export: "data".into(),
    };
    assert_eq!(
        compile(app, catalog).unwrap_err().code,
        "invalid_composition"
    );
}

#[test]
fn protocol_exposes_compilation_without_creating_runtime_state() {
    let (app, catalog) = fixture();
    let mut coordinator = crate::Coordinator::default();
    let request = json!({"protocol": crate::PROTOCOL, "command": "runtime_compose",
        "application": app, "catalog": catalog});
    let first = coordinator.request(request.clone()).unwrap();
    let second = coordinator.request(request).unwrap();
    assert_eq!(first, second);
    assert_eq!(first["order"], json!(["database", "api"]));
}
