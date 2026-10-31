use serde_json::{Value, json};
use std::io::{self, BufRead, Write};

fn read(reader: &mut impl BufRead) -> Value {
    let mut line = String::new();
    assert_ne!(reader.read_line(&mut line).unwrap(), 0);
    let frame: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(frame["protocol"], plugin_host::PROTOCOL);
    frame
}

fn send(writer: &mut impl Write, frame: Value) {
    serde_json::to_writer(&mut *writer, &frame).unwrap();
    writer.write_all(b"\n").unwrap();
    writer.flush().unwrap();
}

fn response(writer: &mut impl Write, request: &Value, result: Value) {
    send(
        writer,
        json!({"protocol":plugin_host::PROTOCOL,"kind":"response","id":request["id"],"ok":true,"result":result}),
    );
}

fn native(reader: &mut impl BufRead, writer: &mut impl Write, id: u64, mut params: Value) -> Value {
    params["version"] = json!(1);
    params["protocol"] = json!("plugin-runtime/1");
    send(
        writer,
        json!({"protocol":plugin_host::PROTOCOL,"kind":"request","id":id,"method":"native","params":params}),
    );
    let result = read(reader);
    assert_eq!(result["kind"], "response");
    assert_eq!(result["id"], id);
    assert_eq!(result["ok"], true);
    assert_eq!(result["result"]["ok"], true, "{result}");
    result["result"]["result"].clone()
}

fn main() {
    let mut reader = io::stdin().lock();
    let mut writer = io::stdout().lock();
    let hello = read(&mut reader);
    assert_eq!(hello["method"], "handshake");
    response(
        &mut writer,
        &hello,
        json!({"runtime":"plugin-runtime/1","worker":"rust"}),
    );
    let boot = read(&mut reader);
    assert_eq!(boot["method"], "boot");
    let created = native(
        &mut reader,
        &mut writer,
        1,
        json!({"command":"runtime_create","plugins":[]}),
    );
    let host = created["host"].clone();
    response(&mut writer, &boot, json!({"ready":true}));
    let run = read(&mut reader);
    assert_eq!(run["method"], "run");
    let before = native(
        &mut reader,
        &mut writer,
        2,
        json!({"command":"runtime_inspect","host":host}),
    );
    native(
        &mut reader,
        &mut writer,
        3,
        json!({"command":"runtime_drain","host":host,"remove":true}),
    );
    native(
        &mut reader,
        &mut writer,
        4,
        json!({"command":"runtime_collect","host":host}),
    );
    response(
        &mut writer,
        &run,
        json!({"worker":"rust","state":before["state"],"echo":boot["params"],"worker_pid":std::process::id()}),
    );
    let shutdown = read(&mut reader);
    assert_eq!(shutdown["method"], "shutdown");
    response(&mut writer, &shutdown, json!({"stopped":true}));
}
