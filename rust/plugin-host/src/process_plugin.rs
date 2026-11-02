//! JSON-lines SDK for contract providers hosted by the Rust process supervisor.
//!
//! Handlers run concurrently and must check [`CallContext`] during long work.
//! Shutdown cancels and joins all calls before closing the plugin; the supervisor
//! enforces a hard termination deadline for handlers that do not cooperate.

use serde::Serialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{self, BufRead, Write};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const PROTOCOL: &str = "evidence-plugin-process/1";
pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;

#[derive(Clone, Debug, Serialize)]
pub struct PluginError {
    pub code: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status_code: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
}

impl PluginError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            status_code: None,
            details: None,
        }
    }

    pub fn with_status(mut self, status_code: u16) -> Self {
        self.status_code = Some(status_code);
        self
    }

    pub fn with_details(mut self, details: Value) -> Self {
        self.details = Some(details);
        self
    }
}

#[derive(Clone)]
pub struct CallContext {
    pub identity: Value,
    deadline: Instant,
    cancelled: Arc<AtomicBool>,
}

impl CallContext {
    pub fn check(&self) -> Result<(), PluginError> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(PluginError::new(
                "CALL_CANCELLED",
                "call cancellation requested",
            ));
        }
        if Instant::now() >= self.deadline {
            return Err(PluginError::new(
                "CALL_DEADLINE_EXCEEDED",
                "call deadline exceeded",
            ));
        }
        Ok(())
    }

    pub fn remaining(&self) -> Duration {
        self.deadline.saturating_duration_since(Instant::now())
    }
}

pub trait Plugin: Send + Sync + 'static {
    fn services(&self) -> BTreeMap<String, Vec<String>>;
    fn configure(&self, configuration: Value) -> Result<(), PluginError>;
    fn invoke(
        &self,
        service: &str,
        method: &str,
        request: Value,
        context: CallContext,
    ) -> Result<Value, PluginError>;
    fn close(&self) -> Result<(), PluginError> {
        Ok(())
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn send(writer: &mut impl Write, id: &str, result: Result<Value, PluginError>) -> io::Result<()> {
    let response = match result {
        Ok(result) => json!({"protocol": PROTOCOL, "id": id, "ok": true, "result": result}),
        Err(error) => {
            let error = if error.code.is_empty()
                || error
                    .status_code
                    .is_some_and(|status| !(400..=599).contains(&status))
                || error
                    .details
                    .as_ref()
                    .is_some_and(|details| !details.is_object())
            {
                PluginError::new(
                    "INVALID_CONTRACT_ERROR",
                    "Plugin returned an invalid public error",
                )
                .with_status(500)
                .with_details(json!({}))
            } else {
                error
            };
            json!({"protocol": PROTOCOL, "id": id, "ok": false, "error": error})
        }
    };
    let mut bytes = serde_json::to_vec(&response).map_err(io::Error::other)?;
    if bytes.len() + 1 > MAX_FRAME_BYTES {
        return send(
            writer,
            id,
            Err(PluginError::new(
                "PROCESS_MESSAGE_LIMIT",
                "response exceeds limit",
            )),
        );
    }
    bytes.push(b'\n');
    writer.write_all(&bytes)?;
    writer.flush()
}

fn read(reader: &mut impl BufRead) -> io::Result<Option<Value>> {
    let Some(bytes) = super::read_frame(reader)? else {
        return Ok(None);
    };
    if bytes.len() + 1 > MAX_FRAME_BYTES {
        return Err(invalid("request exceeds limit"));
    }
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(io::Error::other)
}

fn request_identity(request: &Value) -> io::Result<(&str, &str)> {
    if request["protocol"] != PROTOCOL {
        return Err(invalid("unsupported process protocol"));
    }
    let id = request["id"]
        .as_str()
        .filter(|id| !id.is_empty() && id.len() <= 256)
        .ok_or_else(|| invalid("request id must be a nonempty bounded string"))?;
    let kind = request["kind"]
        .as_str()
        .ok_or_else(|| invalid("request kind is required"))?;
    let base = [
        "protocol",
        "id",
        "kind",
        "identity",
        "deadline_seconds",
        "cancelled",
    ];
    let extra: &[&str] = match kind {
        "handshake" => &["capabilities", "configuration"],
        "invoke" => &["service", "method", "args", "kwargs"],
        "cancel" => &["target"],
        "shutdown" => &[],
        _ => return Err(invalid("unknown request kind")),
    };
    if request.as_object().is_none_or(|object| {
        object
            .keys()
            .any(|key| !base.contains(&key.as_str()) && !extra.contains(&key.as_str()))
    }) {
        return Err(invalid("unknown request field"));
    }
    if !request["identity"].is_object() || !request["cancelled"].is_boolean() {
        return Err(invalid("invalid call identity or cancellation flag"));
    }
    for (key, value) in request["identity"].as_object().unwrap() {
        if key == "deadline" && value.is_null() {
            continue;
        }
        if ![
            "runtime",
            "host",
            "instance",
            "generation",
            "operation",
            "lease",
            "deadline",
        ]
        .contains(&key.as_str())
            || value.as_u64().is_none_or(|number| number == 0)
        {
            return Err(invalid("invalid execution identity field"));
        }
    }
    Ok((id, kind))
}

struct ActiveCall {
    cancelled: Arc<AtomicBool>,
    identity: Value,
}

#[derive(Default)]
struct RequestIds {
    recent: BTreeSet<String>,
    order: VecDeque<String>,
}

impl RequestIds {
    fn insert(&mut self, id: &str) -> bool {
        if !self.recent.insert(id.to_owned()) {
            return false;
        }
        self.order.push_back(id.to_owned());
        if self.order.len() > 1024 {
            self.recent.remove(&self.order.pop_front().unwrap());
        }
        true
    }
}

fn join(workers: &mut Vec<JoinHandle<io::Result<()>>>) -> io::Result<()> {
    let mut failure = None;
    for worker in workers.drain(..) {
        let result = worker
            .join()
            .map_err(|_| io::Error::other("plugin worker panicked"))
            .and_then(|result| result);
        if let Err(error) = result {
            failure.get_or_insert(error);
        }
    }
    failure.map_or(Ok(()), Err)
}

pub fn serve<P: Plugin, R: BufRead, W: Write + Send + 'static>(
    mut reader: R,
    writer: W,
    plugin: P,
) -> io::Result<()> {
    let writer = Arc::new(Mutex::new(writer));
    let plugin = Arc::new(plugin);
    let services = plugin.services();
    if services.is_empty()
        || services.keys().any(String::is_empty)
        || services.values().any(|methods| {
            methods.is_empty()
                || methods
                    .iter()
                    .any(|method| method.is_empty() || method.starts_with('_'))
                || methods.iter().collect::<BTreeSet<_>>().len() != methods.len()
        })
    {
        return Err(invalid("invalid service definitions"));
    }
    let active = Arc::new(Mutex::new(BTreeMap::<String, ActiveCall>::new()));
    let mut workers = Vec::new();
    let mut ready = false;
    let mut identity = Value::Null;
    let mut seen = RequestIds::default();
    let mut shutdown = None;
    let result = (|| {
        while let Some(request) = read(&mut reader)? {
            let (id, kind) = request_identity(&request)?;
            if active.lock().unwrap().contains_key(id) || !seen.insert(id) {
                return Err(invalid("duplicate request id"));
            }
            if ready
                && ["runtime", "instance", "generation"]
                    .iter()
                    .any(|key| request["identity"][key] != identity[key])
            {
                return Err(invalid("stale or foreign execution identity"));
            }
            match kind {
                "handshake" if !ready => {
                    if request["capabilities"] != json!(services) {
                        return Err(invalid("service handshake mismatch"));
                    }
                    let configuration = request
                        .get("configuration")
                        .cloned()
                        .unwrap_or_else(|| json!({}));
                    if !configuration.is_object() {
                        return Err(invalid("configuration must be an object"));
                    }
                    let configured = plugin.configure(configuration);
                    ready = configured.is_ok();
                    identity = request["identity"].clone();
                    let response = configured.map(|()| json!({"protocol": PROTOCOL, "services": services,
                        "runtime": {"language": "rust", "version": env!("CARGO_PKG_VERSION"),
                            "executable": std::env::current_exe().unwrap_or_default().to_string_lossy()}}));
                    send(&mut *writer.lock().unwrap(), id, response)?;
                    if !ready {
                        return Err(invalid("plugin configuration failed"));
                    }
                }
                "invoke" if ready => {
                    let service = request["service"]
                        .as_str()
                        .ok_or_else(|| invalid("missing service"))?;
                    let method = request["method"]
                        .as_str()
                        .ok_or_else(|| invalid("missing method"))?;
                    if !services
                        .get(service)
                        .is_some_and(|methods| methods.iter().any(|name| name == method))
                        || request["args"]
                            .as_array()
                            .is_none_or(|args| args.len() != 1)
                        || request["kwargs"]
                            .as_object()
                            .is_none_or(|kwargs| !kwargs.is_empty())
                    {
                        send(
                            &mut *writer.lock().unwrap(),
                            id,
                            Err(PluginError::new(
                                "CONTRACT_REQUEST_INVALID",
                                "unknown method or invalid arguments",
                            )),
                        )?;
                        continue;
                    }
                    let seconds = request["deadline_seconds"]
                        .as_f64()
                        .filter(|seconds| {
                            seconds.is_finite() && *seconds > 0.0 && *seconds <= 86400.0
                        })
                        .ok_or_else(|| invalid("invalid call deadline"))?;
                    let context = CallContext {
                        identity: request["identity"].clone(),
                        deadline: Instant::now() + Duration::from_secs_f64(seconds),
                        cancelled: Arc::new(AtomicBool::new(request["cancelled"] == true)),
                    };
                    let mut calls = active.lock().unwrap();
                    if calls.len() >= 256 {
                        return Err(invalid("too many active calls"));
                    }
                    calls.insert(
                        id.to_owned(),
                        ActiveCall {
                            cancelled: context.cancelled.clone(),
                            identity: context.identity.clone(),
                        },
                    );
                    drop(calls);
                    let (id, service, method) =
                        (id.to_owned(), service.to_owned(), method.to_owned());
                    let (plugin, writer, active) = (plugin.clone(), writer.clone(), active.clone());
                    workers.push(thread::Builder::new().spawn(move || {
                        let output = catch_unwind(AssertUnwindSafe(|| {
                            context.check()?;
                            plugin.invoke(&service, &method, request["args"][0].clone(), context)
                        }))
                        .unwrap_or_else(|_| {
                            Err(PluginError::new(
                                "HANDLER_PANICKED",
                                "plugin handler panicked",
                            ))
                        });
                        let sent = send(&mut *writer.lock().unwrap(), &id, output);
                        active.lock().unwrap().remove(&id);
                        sent
                    })?);
                    let mut pending = Vec::new();
                    let mut failure = None;
                    for worker in workers.drain(..) {
                        if worker.is_finished() {
                            let result = worker
                                .join()
                                .map_err(|_| io::Error::other("plugin worker panicked"))
                                .and_then(|result| result);
                            if let Err(error) = result {
                                failure.get_or_insert(error);
                            }
                        } else {
                            pending.push(worker);
                        }
                    }
                    workers = pending;
                    if let Some(error) = failure {
                        return Err(error);
                    }
                }
                "cancel" if ready => {
                    let target = request["target"]
                        .as_str()
                        .ok_or_else(|| invalid("missing cancel target"))?;
                    if let Some(call) = active.lock().unwrap().get(target) {
                        if call.identity != request["identity"] {
                            return Err(invalid("cancellation identity mismatch"));
                        }
                        call.cancelled.store(true, Ordering::Release);
                    }
                }
                "shutdown" if ready => {
                    shutdown = Some(id.to_owned());
                    break;
                }
                _ => return Err(invalid("invalid plugin state")),
            }
        }
        Ok(())
    })();
    for call in active.lock().unwrap().values() {
        call.cancelled.store(true, Ordering::Release);
    }
    let joined = join(&mut workers);
    let closed = plugin.close();
    result?;
    joined?;
    if let Some(id) = shutdown {
        send(
            &mut *writer.lock().unwrap(),
            &id,
            closed.map(|()| Value::Null),
        )?;
    } else {
        closed.map_err(|error| io::Error::other(error.message))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
