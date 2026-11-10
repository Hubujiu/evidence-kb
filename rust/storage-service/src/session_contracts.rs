use super::*;

fn invoke(v: Value) -> Result<Value, StorageError> {
    dispatch(v.as_object().unwrap())
}
fn open_values() -> u64 {
    invoke(json!({"action":"values","limits":{"max_nodes":100,"max_bytes":4096}})).unwrap()["handle"].as_u64().unwrap()
}
fn close(h: u64) {
    invoke(json!({"action":"close","handle":h})).unwrap();
}
fn feed(h: u64, events: Value) -> Result<Value, StorageError> {
    invoke(json!({"action":"feed","handle":h,"events":events}))
}

#[test]
fn table_capacity_expiry_monotonic_identity_failed_consumption_and_values_only() {
    let handles: Vec<_> = (0..64).map(|_| open_values()).collect();
    assert_eq!(
        invoke(json!({"action":"values","limits":{"max_nodes":100,"max_bytes":4096}}))
            .unwrap_err()
            .code,
        "STORAGE_CODEC_BUSY"
    );
    close(handles[0]);
    let next = open_values();
    assert!(next > *handles.last().unwrap());
    assert!(feed(handles[0], json!([])).is_err());
    {
        let mut sessions = SESSIONS.get().unwrap().lock().unwrap();
        sessions.values.get_mut(&next).unwrap().touched = Instant::now() - Duration::from_secs(300);
    }
    assert!(feed(next, json!([])).is_err());
    let replacement = open_values();
    assert!(replacement > next);
    for h in handles.into_iter().skip(1) {
        close(h);
    }
    close(replacement);

    let a = open_values();
    let b = open_values();
    assert!(feed(a, json!([{"type":"invalid"}])).is_err());
    assert!(feed(a, json!([])).is_err());
    assert!(invoke(json!({"action":"finish","handle":a})).is_err());
    assert!(feed(a, json!([])).is_err());
    assert_eq!(
        invoke(json!({"action":"finish","handle":b})).unwrap()["count"],
        0
    );
    assert!(feed(b, json!([])).is_err());
    close(a);
    close(b);

    let a = open_values();
    assert!(invoke(json!({"action":"admit","handle":a})).is_err());
    assert!(invoke(json!({"action":"result","handle":a})).is_err());
    close(a);
    let descriptor = json!({"identity":{"space":{"application_id":"app","owner_id":"owner","space_id":"space"},"dataset_id":"kv"},"contract":{"id":"storage.kv","major":1},"methods":["get","set","delete"]});
    let limits = json!({"request":{"max_nodes":100,"max_bytes":4096},"response":{"max_nodes":100,"max_bytes":4096}});
    let a=invoke(json!({"action":"open","descriptor":descriptor,"method":"get","argc":1,"keywords":[],"limits":limits})).unwrap()["handle"].as_u64().unwrap();
    assert!(invoke(json!({"action":"result","handle":a})).is_err());
    assert!(feed(a, json!([])).is_err());
    close(a);
    let a=invoke(json!({"action":"open","descriptor":descriptor,"method":"get","argc":1,"keywords":[],"limits":limits})).unwrap()["handle"].as_u64().unwrap();
    feed(
        a,
        json!([{"event":"open","type":"string"},{"event":"chunk","value":"key"},{"event":"close"}]),
    )
    .unwrap();
    invoke(json!({"action":"admit","handle":a})).unwrap();
    invoke(json!({"action":"result","handle":a})).unwrap();
    feed(
        a,
        json!([{"event":"open","type":"bytes"},{"event":"chunk","value":"AP8="},{"event":"close"}]),
    )
    .unwrap();
    assert_eq!(
        invoke(json!({"action":"finish","handle":a})).unwrap()["completed"],
        true
    );
    assert!(invoke(json!({"action":"finish","handle":a})).is_err());
    close(a);
    assert!(SESSIONS.get().unwrap().lock().unwrap().values.is_empty());
}
