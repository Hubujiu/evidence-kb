use plugin_runtime::{Coordinator, PROTOCOL, native::OwnedTask};
use serde_json::{Value, json};

fn request(runtime: &mut Coordinator, command: &str, fields: Value) -> Value {
    let mut fields = fields.as_object().unwrap().clone();
    fields.insert("protocol".into(), json!(PROTOCOL));
    fields.insert("command".into(), json!(format!("runtime_{command}")));
    runtime.request(Value::Object(fields)).unwrap()
}

fn complete(runtime: &mut Coordinator, action: &Value, services: &[&str]) {
    request(
        runtime,
        "complete",
        json!({"host":action["host"],"operation":action["operation"],"status":"ok","services":services}),
    );
}

fn main() {
    tokio::runtime::Builder::new_current_thread().build().unwrap().block_on(async {
        let mut runtime = Coordinator::default();
        let host = request(&mut runtime, "create", json!({"plugins":[{"id":"native","version":"1","provides":["counter"],"requires":[]}]}))["host"].as_u64().unwrap();
        let init = request(&mut runtime, "poll", json!({"host":host}))["actions"][0].clone();
        let resource = request(&mut runtime, "resource", json!({"host":host,"operation":init["operation"],"kind":"task"}))["resource"].as_u64().unwrap();
        let task = OwnedTask::spawn(resource, |mut stop| async move { if !*stop.borrow_and_update() { let _ = stop.changed().await; } });
        complete(&mut runtime, &init, &["counter"]);
        let binding = request(&mut runtime, "resolve", json!({"host":host,"service":"counter"}));
        let invoke = request(&mut runtime, "invoke", json!({"host":host,"registration":binding["registration"],"method":"read"}));
        tokio::task::yield_now().await;
        complete(&mut runtime, &invoke, &[]);
        request(&mut runtime, "drain", json!({"host":host}));
        let cleanup = request(&mut runtime, "poll", json!({"host":host}))["actions"].as_array().unwrap().iter().find(|action| action["kind"] == "cleanup").unwrap().clone();
        assert_eq!(cleanup["resource"], task.resource());
        task.finish().await.unwrap();
        complete(&mut runtime, &cleanup, &[]);
        let final_cleanup = request(&mut runtime, "poll", json!({"host":host}))["actions"][0].clone();
        complete(&mut runtime, &final_cleanup, &[]);
        let collected = request(&mut runtime, "collect", json!({"host":host}));
        assert_eq!(collected["snapshot"]["state"], "disposed");
        println!("{}", json!({"state":"disposed","remaining":collected["remaining"]}));
    });
}
