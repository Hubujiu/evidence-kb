use super::*;
use std::io::{Cursor, Read};
use std::sync::mpsc;

#[derive(Clone, Default)]
struct Output(Arc<Mutex<Vec<u8>>>);

impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl Output {
    fn frames(&self) -> Vec<Value> {
        let bytes = self.0.lock().unwrap().clone();
        String::from_utf8(bytes)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }
}

struct GatedInput {
    cursor: Cursor<Vec<u8>>,
    offset: u64,
    entered: Option<mpsc::Receiver<()>>,
}

impl Read for GatedInput {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.cursor.read(bytes)
    }
}

impl BufRead for GatedInput {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        if self.cursor.position() >= self.offset
            && let Some(receiver) = self.entered.take()
        {
            receiver
                .recv_timeout(Duration::from_secs(2))
                .map_err(io::Error::other)?;
        }
        self.cursor.fill_buf()
    }
    fn consume(&mut self, count: usize) {
        self.cursor.consume(count);
    }
}

struct Fixture {
    events: Arc<Mutex<Vec<&'static str>>>,
    entered: mpsc::Sender<()>,
    wait_for_cancel: bool,
}

impl Plugin for Fixture {
    fn services(&self) -> BTreeMap<String, Vec<String>> {
        BTreeMap::from([("value".into(), vec!["get".into()])])
    }
    fn configure(&self, config: Value) -> Result<(), PluginError> {
        if config != json!({}) {
            return Err(PluginError::new("CONFIG", "empty config required")
                .with_status(422)
                .with_details(json!({"field": "configuration"})));
        }
        self.events.lock().unwrap().push("configure");
        Ok(())
    }
    fn invoke(
        &self,
        _: &str,
        _: &str,
        request: Value,
        context: CallContext,
    ) -> Result<Value, PluginError> {
        self.events.lock().unwrap().push("invoke");
        self.entered.send(()).unwrap();
        if self.wait_for_cancel {
            while context.check().is_ok() {
                thread::yield_now();
            }
            self.events.lock().unwrap().push("cancelled");
            context.check()?;
        }
        self.events.lock().unwrap().push("finished");
        Ok(request)
    }
    fn close(&self) -> Result<(), PluginError> {
        self.events.lock().unwrap().push("close");
        Ok(())
    }
}

fn frame(id: &str, kind: &str, fields: Value) -> Vec<u8> {
    let mut value = json!({"protocol": PROTOCOL, "id": id, "kind": kind,
        "identity": {}, "deadline_seconds": 5.0, "cancelled": false});
    value
        .as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    let mut bytes = serde_json::to_vec(&value).unwrap();
    bytes.push(b'\n');
    bytes
}

fn handshake() -> Vec<u8> {
    frame(
        "hello",
        "handshake",
        json!({"capabilities": {"value": ["get"]}, "configuration": {}}),
    )
}

fn invoke(id: &str) -> Vec<u8> {
    frame(
        id,
        "invoke",
        json!({"service":"value", "method":"get", "args":[{"answer": 42}], "kwargs":{}}),
    )
}

fn identified(bytes: Vec<u8>, identity: Value) -> Vec<u8> {
    let mut request: Value = serde_json::from_slice(&bytes).unwrap();
    request["identity"] = identity;
    let mut bytes = serde_json::to_vec(&request).unwrap();
    bytes.push(b'\n');
    bytes
}

#[test]
fn lifecycle_requires_one_matching_handshake() {
    let bad_handshake = frame(
        "hello",
        "handshake",
        json!({
            "capabilities": {"value": ["unknown"]}, "configuration": {}
        }),
    );
    let repeated = [
        handshake(),
        frame(
            "again",
            "handshake",
            json!({
                "capabilities": {"value": ["get"]}, "configuration": {}
            }),
        ),
    ]
    .concat();
    for (bytes, expected) in [
        (bad_handshake, vec!["close"]),
        (invoke("early"), vec!["close"]),
        (frame("stop", "shutdown", json!({})), vec!["close"]),
        (repeated, vec!["configure", "close"]),
    ] {
        let (sender, _) = mpsc::channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        assert!(
            serve(
                Cursor::new(bytes),
                Output::default(),
                Fixture {
                    events: events.clone(),
                    entered: sender,
                    wait_for_cancel: false
                }
            )
            .is_err()
        );
        assert_eq!(*events.lock().unwrap(), expected);
    }
}

#[test]
fn stale_or_malformed_execution_identity_never_reaches_handler() {
    let owner = json!({"runtime":1, "host":2, "instance":3, "generation":4, "operation":5});
    for identity in [
        json!({"runtime":1, "host":2, "instance":3, "generation":3, "operation":6, "lease":7, "deadline":null}),
        json!({"runtime":9, "host":2, "instance":3, "generation":4, "operation":6, "lease":7, "deadline":null}),
        json!({"runtime":1, "host":2, "instance":3, "generation":4, "operation":false}),
    ] {
        let (sender, _) = mpsc::channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let bytes = [
            identified(handshake(), owner.clone()),
            identified(invoke("request"), identity),
        ]
        .concat();
        assert!(
            serve(
                Cursor::new(bytes),
                Output::default(),
                Fixture {
                    events: events.clone(),
                    entered: sender,
                    wait_for_cancel: false
                }
            )
            .is_err()
        );
        assert_eq!(*events.lock().unwrap(), ["configure", "close"]);
    }
}

#[test]
fn valid_caller_scope_change_keeps_provider_identity() {
    let owner = json!({"runtime":1, "host":2, "instance":3, "generation":4, "operation":5});
    let caller = json!({"runtime":1, "host":20, "instance":3, "generation":4, "operation":6, "lease":7, "deadline":null});
    let (sender, receiver) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let bytes = [
        identified(handshake(), owner.clone()),
        identified(invoke("request"), caller),
    ]
    .concat();
    let offset = bytes.len() as u64;
    let bytes = [
        bytes,
        identified(frame("stop", "shutdown", json!({})), owner),
    ]
    .concat();
    serve(
        GatedInput {
            cursor: Cursor::new(bytes),
            offset,
            entered: Some(receiver),
        },
        Output::default(),
        Fixture {
            events: events.clone(),
            entered: sender,
            wait_for_cancel: false,
        },
    )
    .unwrap();
    assert_eq!(
        *events.lock().unwrap(),
        ["configure", "invoke", "finished", "close"]
    );
}

#[test]
fn wrong_cancellation_identity_fails_and_drains_call() {
    let (sender, receiver) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let bytes = [handshake(), invoke("request")].concat();
    let offset = bytes.len() as u64;
    let bytes = [
        bytes,
        identified(
            frame("cancel", "cancel", json!({"target":"request"})),
            json!({"operation": 10}),
        ),
    ]
    .concat();
    assert!(
        serve(
            GatedInput {
                cursor: Cursor::new(bytes),
                offset,
                entered: Some(receiver)
            },
            Output::default(),
            Fixture {
                events: events.clone(),
                entered: sender,
                wait_for_cancel: true
            }
        )
        .is_err()
    );
    assert_eq!(
        *events.lock().unwrap(),
        ["configure", "invoke", "cancelled", "close"]
    );
}

#[test]
fn invokes_json_handler_and_closes_after_completion() {
    let (sender, receiver) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let output = Output::default();
    let bytes = [handshake(), invoke("request")].concat();
    let offset = bytes.len() as u64;
    let bytes = [bytes, frame("stop", "shutdown", json!({}))].concat();
    serve(
        GatedInput {
            cursor: Cursor::new(bytes),
            offset,
            entered: Some(receiver),
        },
        output.clone(),
        Fixture {
            events: events.clone(),
            entered: sender,
            wait_for_cancel: false,
        },
    )
    .unwrap();
    assert_eq!(
        *events.lock().unwrap(),
        ["configure", "invoke", "finished", "close"]
    );
    let frames = output.frames();
    assert_eq!(frames[0]["result"]["runtime"]["language"], "rust");
    assert_eq!(frames[1]["result"], json!({"answer": 42}));
    assert_eq!(frames[2]["id"], "stop");
}

#[test]
fn cancellation_waits_for_handler_before_close_and_shutdown_response() {
    let (sender, receiver) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let output = Output::default();
    let bytes = [handshake(), invoke("request")].concat();
    let offset = bytes.len() as u64;
    let bytes = [
        bytes,
        frame("cancel", "cancel", json!({"target":"request"})),
        frame("stop", "shutdown", json!({})),
    ]
    .concat();
    serve(
        GatedInput {
            cursor: Cursor::new(bytes),
            offset,
            entered: Some(receiver),
        },
        output.clone(),
        Fixture {
            events: events.clone(),
            entered: sender,
            wait_for_cancel: true,
        },
    )
    .unwrap();
    assert_eq!(
        *events.lock().unwrap(),
        ["configure", "invoke", "cancelled", "close"]
    );
    let frames = output.frames();
    assert_eq!(frames[1]["error"]["code"], "CALL_CANCELLED");
    assert_eq!(frames[2]["id"], "stop");
}

#[test]
fn unknown_method_is_rejected_without_execution() {
    let (sender, _) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let output = Output::default();
    let bytes = [
        handshake(),
        frame(
            "request",
            "invoke",
            json!({
        "service":"value", "method":"private", "args":[{}], "kwargs":{}}),
        ),
        frame("stop", "shutdown", json!({})),
    ]
    .concat();
    serve(
        Cursor::new(bytes),
        output.clone(),
        Fixture {
            events: events.clone(),
            entered: sender,
            wait_for_cancel: false,
        },
    )
    .unwrap();
    assert_eq!(*events.lock().unwrap(), ["configure", "close"]);
    assert_eq!(
        output.frames()[1]["error"]["code"],
        "CONTRACT_REQUEST_INVALID"
    );
}

#[test]
fn invalid_protocol_fails_without_configuration_or_execution() {
    let (sender, _) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let bytes = b"{\"protocol\":\"other\",\"id\":\"hello\",\"kind\":\"handshake\"}\n";
    assert!(
        serve(
            Cursor::new(bytes),
            Output::default(),
            Fixture {
                events: events.clone(),
                entered: sender,
                wait_for_cancel: false
            }
        )
        .is_err()
    );
    assert_eq!(*events.lock().unwrap(), ["close"]);
}

#[test]
fn configuration_error_preserves_public_error_fields() {
    let (sender, _) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let output = Output::default();
    let bytes = frame(
        "hello",
        "handshake",
        json!({
            "capabilities": {"value": ["get"]}, "configuration": {"unknown": true}
        }),
    );
    assert!(
        serve(
            Cursor::new(bytes),
            output.clone(),
            Fixture {
                events: events.clone(),
                entered: sender,
                wait_for_cancel: false
            }
        )
        .is_err()
    );
    assert_eq!(*events.lock().unwrap(), ["close"]);
    assert_eq!(
        output.frames()[0]["error"],
        json!({
            "code": "CONFIG", "message": "empty config required", "status_code": 422,
            "details": {"field": "configuration"}
        })
    );
}

#[test]
fn malformed_public_error_is_replaced_before_serialization() {
    for error in [
        PluginError::new("INVALID_STATUS", "bad").with_status(200),
        PluginError::new("INVALID_STATUS", "bad").with_status(600),
        PluginError::new("INVALID_DETAILS", "bad").with_details(json!([])),
        PluginError::new("", "bad"),
    ] {
        let mut output = Output::default();
        send(&mut output, "request", Err(error)).unwrap();
        assert_eq!(
            output.frames()[0]["error"],
            json!({
                "code": "INVALID_CONTRACT_ERROR", "message": "Plugin returned an invalid public error",
                "status_code": 500, "details": {}
            })
        );
    }
}

#[test]
fn expired_context_and_initial_cancellation_are_observable() {
    let cancelled = Arc::new(AtomicBool::new(false));
    let context = CallContext {
        identity: json!({}),
        deadline: Instant::now(),
        cancelled: cancelled.clone(),
    };
    assert_eq!(context.remaining(), Duration::ZERO);
    assert_eq!(context.check().unwrap_err().code, "CALL_DEADLINE_EXCEEDED");
    cancelled.store(true, Ordering::Release);
    assert_eq!(context.check().unwrap_err().code, "CALL_CANCELLED");
}

#[test]
fn request_identity_replay_cache_is_bounded() {
    let mut ids = RequestIds::default();
    for number in 0..2050 {
        assert!(ids.insert(&number.to_string()));
    }
    assert_eq!(ids.order.len(), 1024);
    assert_eq!(ids.recent.len(), 1024);
    assert!(!ids.insert("2049"));
    assert!(ids.insert("0"));
    assert_eq!(ids.recent.len(), 1024);
}

#[test]
fn active_identity_stays_protected_after_replay_cache_eviction() {
    let (sender, receiver) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let mut bytes = [handshake(), invoke("request")].concat();
    let offset = bytes.len() as u64;
    for number in 0..1025 {
        bytes.extend(frame(
            &format!("cancel-{number}"),
            "cancel",
            json!({"target":"absent"}),
        ));
    }
    bytes.extend(invoke("request"));
    assert!(
        serve(
            GatedInput {
                cursor: Cursor::new(bytes),
                offset,
                entered: Some(receiver)
            },
            Output::default(),
            Fixture {
                events: events.clone(),
                entered: sender,
                wait_for_cancel: true
            }
        )
        .is_err()
    );
    assert_eq!(
        *events.lock().unwrap(),
        ["configure", "invoke", "cancelled", "close"]
    );
}

#[test]
fn repeated_invoke_identity_never_executes_twice() {
    let (sender, receiver) = mpsc::channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let bytes = [handshake(), invoke("request")].concat();
    let offset = bytes.len() as u64;
    let bytes = [bytes, invoke("request")].concat();
    assert!(
        serve(
            GatedInput {
                cursor: Cursor::new(bytes),
                offset,
                entered: Some(receiver)
            },
            Output::default(),
            Fixture {
                events: events.clone(),
                entered: sender,
                wait_for_cancel: false
            }
        )
        .is_err()
    );
    assert_eq!(
        *events.lock().unwrap(),
        ["configure", "invoke", "finished", "close"]
    );
}
