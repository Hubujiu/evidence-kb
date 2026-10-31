use super::*;

fn config(mode: &str, direct_return: bool) -> LoopConfig {
    LoopConfig {
        mode: mode.into(),
        message: json!("hello"),
        tools: vec![json!({"name": "echo", "direct_return": direct_return})],
        tool_name: (mode == "single-tool").then(|| "echo".into()),
        arguments: (mode == "single-tool")
            .then(|| json!({"text": "hello"}).as_object().unwrap().clone()),
    }
}

fn create(registry: &mut LoopRegistry, mode: &str, direct_return: bool) -> String {
    registry.create(config(mode, direct_return)).unwrap()["loop_id"]
        .as_str()
        .unwrap()
        .into()
}

fn effect(registry: &mut LoopRegistry, id: &str, name: &str, result: Value) -> Value {
    let instruction = serde_json::to_value(registry.next(id).unwrap()).unwrap();
    assert_eq!(instruction["name"], name);
    registry
        .accept(id, instruction["effect_id"].as_u64().unwrap(), result)
        .unwrap();
    instruction["arguments"].clone()
}

fn enter_model(registry: &mut LoopRegistry, id: &str) {
    effect(registry, id, "session.start", json!({}));
    effect(
        registry,
        id,
        "context.prepare",
        json!({"messages": [{"role": "user", "content": "prepared"}], "tools": []}),
    );
}

#[test]
fn session_and_context_services_own_history_and_render_each_model_request() {
    let mut registry = LoopRegistry::with_builtins();
    let id = create(&mut registry, "model-tools", false);
    let mut history: Vec<Value> = vec![];
    let mut model_calls = 0;
    let mut context_calls = 0;
    let mut tool_calls = 0;
    loop {
        let instruction = serde_json::to_value(registry.next(&id).unwrap()).unwrap();
        if instruction["kind"] == "complete" {
            assert_eq!(instruction["value"], json!("done"));
            break;
        }
        let arguments = &instruction["arguments"];
        let result = match instruction["name"].as_str().unwrap() {
            "session.start" => {
                history.push(json!({"role": "user", "content": arguments["message"]}));
                json!({})
            }
            "context.prepare" => {
                context_calls += 1;
                json!({"messages": history, "tools": [{"name": "echo"}], "context_revision": context_calls})
            }
            "decide" => {
                model_calls += 1;
                assert_eq!(arguments["messages"], json!(history));
                assert_eq!(arguments["context_revision"], json!(model_calls));
                if model_calls == 1 {
                    json!({"kind": "tool", "name": "echo", "arguments": {"text": "hello"}})
                } else {
                    json!({"kind": "answer", "value": "done"})
                }
            }
            "session.append" => {
                history.extend(arguments["records"].as_array().unwrap().clone());
                Value::Null
            }
            "echo" => {
                tool_calls += 1;
                assert_eq!(arguments, &json!({"text": "hello"}));
                json!({"echo": "hello"})
            }
            name => panic!("unexpected service {name}"),
        };
        registry
            .accept(&id, instruction["effect_id"].as_u64().unwrap(), result)
            .unwrap();
    }
    assert_eq!((context_calls, model_calls, tool_calls), (2, 2, 1));
    assert_eq!(
        history
            .iter()
            .map(|item| item["role"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["user", "assistant", "tool", "assistant"]
    );
    assert_eq!(history.last().unwrap()["content"], "done");
    assert!(registry.snapshot(&id).unwrap().get("messages").is_none());
}

#[test]
fn direct_tool_delivery_is_recorded_before_completion_without_model_rewrite() {
    let mut registry = LoopRegistry::with_builtins();
    let id = create(&mut registry, "model-tools", true);
    enter_model(&mut registry, &id);
    effect(
        &mut registry,
        &id,
        "decide",
        json!({"kind": "tool", "name": "echo", "arguments": {}}),
    );
    let call = effect(&mut registry, &id, "session.append", Value::Null);
    assert_eq!(call["records"][0]["tool_call"]["name"], "echo");
    let answer = json!({"answer": "verified", "citations": ["source:1"]});
    effect(&mut registry, &id, "echo", answer.clone());
    let records = effect(&mut registry, &id, "session.append", Value::Null);
    assert_eq!(
        records["records"][1],
        json!({"role": "assistant", "content": answer})
    );
    assert_eq!(
        registry.next(&id).unwrap(),
        Instruction::Complete { value: answer }
    );
}

#[test]
fn single_tool_policy_requires_no_model_or_context_service() {
    let mut registry = LoopRegistry::with_builtins();
    let id = create(&mut registry, "single-tool", false);
    effect(&mut registry, &id, "session.start", json!({}));
    effect(&mut registry, &id, "session.append", Value::Null);
    effect(&mut registry, &id, "echo", json!({"value": 42}));
    effect(&mut registry, &id, "session.append", Value::Null);
    assert_eq!(
        registry.next(&id).unwrap(),
        Instruction::Complete {
            value: json!({"value": 42})
        }
    );
}

#[test]
fn an_issued_effect_cannot_be_replayed_and_protocol_failure_closes_the_loop() {
    let mut registry = LoopRegistry::with_builtins();
    let id = create(&mut registry, "single-tool", false);
    registry.next(&id).unwrap();
    assert_eq!(registry.next(&id).unwrap_err().code, "LOOP_EFFECT_PENDING");
    assert_eq!(registry.next(&id).unwrap_err().code, "LOOP_NOT_FOUND");
}

#[test]
fn a_late_result_cannot_complete_a_different_effect_or_a_closed_loop() {
    let mut registry = LoopRegistry::with_builtins();
    let id = create(&mut registry, "model-tools", false);
    registry.next(&id).unwrap();
    assert_eq!(
        registry.accept(&id, 99, Value::Null).unwrap_err().code,
        "LOOP_CORRELATION"
    );
    assert_eq!(
        registry.accept(&id, 1, Value::Null).unwrap_err().code,
        "LOOP_NOT_FOUND"
    );
    let next_id = create(&mut registry, "model-tools", false);
    assert_ne!(id, next_id);
    assert_eq!(registry.close(&next_id), json!({"closed": true}));
    assert_eq!(registry.close(&next_id), json!({"closed": false}));
}

#[test]
fn unavailable_tools_and_malformed_model_decisions_fail_closed() {
    for decision in [
        json!({"kind": "tool", "name": "missing", "arguments": {}}),
        json!({"kind": "tool", "name": "echo", "arguments": []}),
        json!({"kind": "answer"}),
        json!({"kind": "answer", "value": "x", "unrecognized": true}),
    ] {
        let mut registry = LoopRegistry::with_builtins();
        let id = create(&mut registry, "model-tools", false);
        enter_model(&mut registry, &id);
        let instruction = serde_json::to_value(registry.next(&id).unwrap()).unwrap();
        assert!(
            registry
                .accept(&id, instruction["effect_id"].as_u64().unwrap(), decision)
                .is_err()
        );
        assert_eq!(registry.next(&id).unwrap_err().code, "LOOP_NOT_FOUND");
    }
}

#[test]
fn malformed_context_does_not_reach_the_model() {
    let mut registry = LoopRegistry::with_builtins();
    let id = create(&mut registry, "model-tools", false);
    effect(&mut registry, &id, "session.start", json!({}));
    let instruction = serde_json::to_value(registry.next(&id).unwrap()).unwrap();
    assert_eq!(
        registry
            .accept(
                &id,
                instruction["effect_id"].as_u64().unwrap(),
                json!({"messages": []})
            )
            .unwrap_err()
            .code,
        "INVALID_CONTEXT"
    );
    assert_eq!(registry.next(&id).unwrap_err().code, "LOOP_NOT_FOUND");
}

#[test]
fn profile_tool_selection_and_manifest_are_validated_before_start() {
    let mut registry = LoopRegistry::with_builtins();
    let mut missing = config("single-tool", false);
    missing.tool_name = Some("not-registered".into());
    assert_eq!(registry.create(missing).unwrap_err().code, "UNKNOWN_TOOL");
    let mut duplicate = config("model-tools", false);
    duplicate.tools.push(duplicate.tools[0].clone());
    assert_eq!(
        registry.create(duplicate).unwrap_err().code,
        "DUPLICATE_TOOL"
    );
    let mut invalid = config("single-tool", false);
    invalid.arguments = None;
    assert_eq!(
        registry.create(invalid).unwrap_err().code,
        "INVALID_LOOP_CONFIG"
    );
}

struct ConstantLoop;

impl LoopProgram for ConstantLoop {
    fn next(&mut self) -> Result<Instruction, Error> {
        Ok(Instruction::Complete {
            value: json!("alternate policy"),
        })
    }
    fn accept(&mut self, _: u64, _: Value) -> Result<(), Error> {
        Err(Error::new("LOOP_TERMINAL", "no effects"))
    }
    fn snapshot(&self) -> Value {
        json!({"phase": "complete"})
    }
}

#[test]
fn a_loop_plugin_can_replace_the_policy_without_changing_the_host() {
    let mut registry = LoopRegistry::new();
    registry
        .register("constant", |_| Ok(Box::new(ConstantLoop)))
        .unwrap();
    let mut configuration = config("model-tools", false);
    configuration.mode = "constant".into();
    let id = registry.create(configuration).unwrap()["loop_id"]
        .as_str()
        .unwrap()
        .to_owned();
    assert_eq!(
        registry.next(&id).unwrap(),
        Instruction::Complete {
            value: json!("alternate policy")
        }
    );
    assert!(
        registry
            .register("constant", |_| Ok(Box::new(ConstantLoop)))
            .is_err()
    );
}

#[test]
fn bridge_rejects_unknown_fields_and_handles_null_answers() {
    let request = json!({"version": 1, "command": "loop_create", "mode": "model-tools",
        "message": "x", "tools": [], "history": []});
    assert_eq!(
        crate::call(request.to_string().as_bytes())["error"]["code"],
        "INVALID_REQUEST"
    );
    let decision: Decision =
        serde_json::from_value(json!({"kind": "answer", "value": null})).unwrap();
    assert!(matches!(decision, Decision::Answer { value: Value::Null }));
}

#[test]
fn session_cache_short_circuits_all_new_model_tool_and_append_effects() {
    for mode in ["model-tools", "single-tool"] {
        for cached in [json!({"answer": "already complete"}), Value::Null] {
            let mut registry = LoopRegistry::with_builtins();
            let id = create(&mut registry, mode, false);
            effect(
                &mut registry,
                &id,
                "session.start",
                json!({"cached": cached}),
            );
            assert_eq!(
                registry.next(&id).unwrap(),
                Instruction::Complete { value: cached }
            );
            assert_eq!(registry.snapshot(&id).unwrap()["tool_count"], 0);
        }
    }
}
