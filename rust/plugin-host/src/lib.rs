use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs;
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread;
use std::time::{Duration, Instant};

mod process_tree;
use process_tree::ProcessTree;
mod shutdown_signals;
pub use shutdown_signals::ShutdownSignals;

pub const PROTOCOL: &str = "plugin-host/1";
pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024 + 64 * 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LaunchProfile {
    pub version: u64,
    pub worker: Vec<String>,
    pub cwd: PathBuf,
    pub application: Value,
    #[serde(default)]
    pub stop_file: Option<PathBuf>,
    #[serde(default = "startup_timeout")]
    pub startup_timeout_ms: u64,
    #[serde(default = "shutdown_timeout")]
    pub shutdown_timeout_ms: u64,
}

fn startup_timeout() -> u64 {
    30_000
}
fn shutdown_timeout() -> u64 {
    10_000
}

impl LaunchProfile {
    pub fn load(path: impl AsRef<Path>) -> Result<Self, HostError> {
        let path = fs::canonicalize(path).map_err(|e| HostError::new("profile", e.to_string()))?;
        let mut profile: Self = serde_json::from_slice(
            &fs::read(&path).map_err(|e| HostError::new("profile", e.to_string()))?,
        )
        .map_err(|e| HostError::new("profile", e.to_string()))?;
        if profile.cwd.is_relative() {
            profile.cwd = path.parent().unwrap().join(&profile.cwd);
        }
        if let Some(stop_file) = &mut profile.stop_file
            && stop_file.is_relative()
        {
            *stop_file = path.parent().unwrap().join(&*stop_file);
        }
        profile.cwd =
            fs::canonicalize(&profile.cwd).map_err(|e| HostError::new("profile", e.to_string()))?;
        profile.validate()?;
        Ok(profile)
    }

    fn validate(&self) -> Result<(), HostError> {
        if self.version != 1
            || self.worker.is_empty()
            || self.worker[0].is_empty()
            || self.startup_timeout_ms == 0
            || self.shutdown_timeout_ms == 0
        {
            return Err(HostError::new(
                "profile",
                "expected version 1, worker argv and positive timeouts",
            ));
        }
        if Instant::now()
            .checked_add(Duration::from_millis(self.startup_timeout_ms))
            .is_none()
            || Instant::now()
                .checked_add(Duration::from_millis(self.shutdown_timeout_ms))
                .is_none()
        {
            return Err(HostError::new("profile", "timeout is out of range"));
        }
        Ok(())
    }
}

#[derive(Debug, Serialize)]
pub struct HostError {
    pub code: String,
    pub message: String,
    pub roots: Vec<Value>,
}

impl HostError {
    fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            roots: Vec::new(),
        }
    }
}

impl fmt::Display for HostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}",
            serde_json::to_string(self).map_err(|_| fmt::Error)?
        )
    }
}
impl std::error::Error for HostError {}

#[derive(Debug, Serialize)]
pub struct HostResult {
    pub status: &'static str,
    pub result: Value,
    pub runtime_owner_pid: u32,
}

enum Event {
    Frame(Value),
    Written(u64),
    Failed(String),
    Eof,
    Stop,
}

fn read_frame(reader: &mut impl BufRead) -> io::Result<Option<Vec<u8>>> {
    let mut frame = Vec::new();
    loop {
        let buffer = reader.fill_buf()?;
        if buffer.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "unterminated JSONL frame",
                ))
            };
        }
        let end = buffer.iter().position(|byte| *byte == b'\n');
        let length = end.unwrap_or(buffer.len());
        if frame.len() + length > MAX_FRAME_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "frame exceeds limit",
            ));
        }
        frame.extend_from_slice(&buffer[..length]);
        reader.consume(length + usize::from(end.is_some()));
        if end.is_some() {
            return Ok(Some(frame));
        }
    }
}

struct Worker {
    process: ProcessTree,
    outgoing: Option<SyncSender<(u64, Vec<u8>)>>,
    incoming: Receiver<Event>,
    writes: BTreeMap<u64, Instant>,
    next_write: u64,
    write_timeout: Duration,
    stop_file: Option<PathBuf>,
    watch_stop: bool,
}

impl Worker {
    fn spawn(profile: &LaunchProfile) -> Result<Self, HostError> {
        let executable = Path::new(&profile.worker[0]);
        let executable = if executable.is_relative() && executable.components().count() > 1 {
            profile.cwd.join(executable)
        } else {
            executable.to_owned()
        };
        let mut command = Command::new(executable);
        command
            .args(&profile.worker[1..])
            .current_dir(&profile.cwd)
            .env(
                "PLUGIN_HOST_EXECUTABLE",
                std::env::current_exe()
                    .map_err(|e| HostError::new("worker_spawn", e.to_string()))?,
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        let mut process = ProcessTree::spawn(&mut command)
            .map_err(|e| HostError::new("worker_spawn", e.to_string()))?;
        let stdout = process.child.stdout.take().unwrap();
        let mut stdin = process.child.stdin.take().unwrap();
        let (events, incoming) = mpsc::sync_channel(32);
        let reader_events = events.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let event = match read_frame(&mut reader) {
                    Ok(Some(bytes)) => match serde_json::from_slice(&bytes) {
                        Ok(value) => Event::Frame(value),
                        Err(error) => Event::Failed(format!("invalid JSON: {error}")),
                    },
                    Ok(None) => Event::Eof,
                    Err(error) => Event::Failed(error.to_string()),
                };
                let terminal = matches!(event, Event::Failed(_) | Event::Eof);
                if reader_events.send(event).is_err() || terminal {
                    break;
                }
            }
        });
        let (outgoing, queued) = mpsc::sync_channel::<(u64, Vec<u8>)>(32);
        thread::spawn(move || {
            while let Ok((id, bytes)) = queued.recv() {
                let event = match stdin.write_all(&bytes).and_then(|()| stdin.flush()) {
                    Ok(()) => Event::Written(id),
                    Err(error) => Event::Failed(format!("worker write: {error}")),
                };
                let terminal = matches!(event, Event::Failed(_));
                if events.send(event).is_err() || terminal {
                    break;
                }
            }
        });
        Ok(Self {
            process,
            outgoing: Some(outgoing),
            incoming,
            writes: BTreeMap::new(),
            next_write: 0,
            write_timeout: Duration::from_millis(profile.shutdown_timeout_ms),
            stop_file: None,
            watch_stop: false,
        })
    }

    fn send(&mut self, value: Value) -> Result<(), HostError> {
        let mut bytes =
            serde_json::to_vec(&value).map_err(|e| HostError::new("protocol", e.to_string()))?;
        if bytes.len() > MAX_FRAME_BYTES {
            return Err(HostError::new("protocol", "outgoing frame exceeds limit"));
        }
        bytes.push(b'\n');
        self.next_write += 1;
        self.outgoing
            .as_ref()
            .unwrap()
            .try_send((self.next_write, bytes))
            .map_err(|e| HostError::new("transport", format!("worker write queue: {e}")))?;
        self.writes.insert(self.next_write, Instant::now());
        Ok(())
    }

    fn event(&mut self, deadline: Option<Instant>) -> Result<Event, HostError> {
        loop {
            if self.watch_stop
                && (shutdown_signals::take_stop_request()
                    || self.stop_file.as_ref().is_some_and(|path| path.exists()))
            {
                self.stop_file.take();
                return Ok(Event::Stop);
            }
            if deadline.is_some_and(|end| Instant::now() >= end) {
                return Err(HostError::new(
                    "timeout",
                    "worker lifecycle deadline exceeded",
                ));
            }
            if self
                .writes
                .values()
                .any(|start| start.elapsed() >= self.write_timeout)
            {
                return Err(HostError::new(
                    "timeout",
                    "worker stopped reading the protocol pipe",
                ));
            }
            match self.incoming.recv_timeout(Duration::from_millis(20)) {
                Ok(Event::Written(id)) => {
                    self.writes.remove(&id);
                }
                Ok(event) => return Ok(event),
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(HostError::new("transport", "worker transport disconnected"));
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if let Some(status) = self
                        .process
                        .try_wait()
                        .map_err(|e| HostError::new("worker_exit", e.to_string()))?
                    {
                        return Err(HostError::new(
                            "worker_exit",
                            format!("worker exited before completing protocol: {status}"),
                        ));
                    }
                }
            }
        }
    }

    fn reap(&mut self, deadline: Instant) -> Result<(), HostError> {
        self.outgoing.take();
        loop {
            if let Some(status) = self
                .process
                .try_wait()
                .map_err(|e| HostError::new("worker_exit", e.to_string()))?
            {
                return if status.success() {
                    Ok(())
                } else {
                    Err(HostError::new("worker_exit", status.to_string()))
                };
            }
            if Instant::now() >= deadline {
                return Err(HostError::new(
                    "timeout",
                    "worker did not exit after shutdown",
                ));
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}

pub struct Host;

impl Host {
    pub fn run(
        profile: LaunchProfile,
        dispatch: impl Fn(&[u8]) -> Value,
    ) -> Result<HostResult, HostError> {
        profile.validate()?;
        let mut worker = Worker::spawn(&profile)?;
        let mut roots = BTreeSet::new();
        let outcome = (|| {
            let startup = Instant::now() + Duration::from_millis(profile.startup_timeout_ms);
            let hello = exchange(
                &mut worker,
                &dispatch,
                &mut roots,
                1,
                "handshake",
                json!({"runtime":"plugin-runtime/1"}),
                Some(startup),
            )?;
            if hello["runtime"] != "plugin-runtime/1" {
                return Err(HostError::new(
                    "handshake",
                    "worker runtime protocol mismatch",
                ));
            }
            let boot = exchange(
                &mut worker,
                &dispatch,
                &mut roots,
                2,
                "boot",
                profile.application,
                Some(startup),
            )?;
            if boot["ready"] != true {
                return Err(HostError::new("boot", "worker did not report ready"));
            }
            for root in &roots {
                let state = inspect(&dispatch, *root);
                if state["ok"] != true || state["result"]["state"] != "ready" {
                    return Err(HostError::new(
                        "boot",
                        format!("runtime root {root} is not ready"),
                    ));
                }
            }
            eprintln!(
                "{}",
                json!({"status":"ready","runtime_owner_pid":std::process::id(),"roots":roots})
            );
            worker.stop_file = profile.stop_file;
            worker.watch_stop = true;
            worker.send(
                json!({"protocol":PROTOCOL,"kind":"request","id":3,"method":"run","params":{}}),
            )?;
            let mut result = None;
            let mut shutdown = None;
            let mut stopped = false;
            while result.is_none() || !stopped {
                match next_response(&mut worker, &dispatch, &mut roots, shutdown)? {
                    Some((3, response)) if result.is_none() => {
                        result = Some(response);
                    }
                    Some((4, _)) if shutdown.is_some() && !stopped => {
                        stopped = true;
                    }
                    Some(_) => {
                        return Err(HostError::new(
                            "protocol",
                            "unexpected lifecycle response ID",
                        ));
                    }
                    None => {}
                }
                if shutdown.is_none() && (result.is_some() || worker.stop_file.is_none()) {
                    shutdown =
                        Some(Instant::now() + Duration::from_millis(profile.shutdown_timeout_ms));
                    worker.stop_file.take();
                    worker.send(json!({"protocol":PROTOCOL,"kind":"request","id":4,"method":"shutdown","params":{}}))?;
                }
            }
            if !roots.is_empty() {
                return Err(HostError::new(
                    "runtime_leak",
                    "shutdown left uncollected runtime roots",
                ));
            }
            worker.reap(shutdown.unwrap())?;
            Ok(HostResult {
                status: "completed",
                result: result.unwrap(),
                runtime_owner_pid: std::process::id(),
            })
        })();
        worker.outgoing.take();
        let cleanup = worker
            .process
            .shutdown(Instant::now() + Duration::from_millis(profile.shutdown_timeout_ms));
        let outcome = match (outcome, cleanup) {
            (outcome, Ok(())) => outcome,
            (Ok(_), Err(error)) => Err(HostError::new("worker_cleanup", error.to_string())),
            (Err(mut error), Err(cleanup)) => {
                error.message =
                    format!("{}; process tree cleanup failed: {cleanup}", error.message);
                Err(error)
            }
        };
        outcome.map_err(|mut error| {
            error.roots = roots
                .into_iter()
                .map(|root| json!({"host":root,"snapshot":inspect(&dispatch, root)}))
                .collect();
            error
        })
    }
}

fn dispatch_call(dispatch: &impl Fn(&[u8]) -> Value, request: &Value) -> Value {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| dispatch(&serde_json::to_vec(request).unwrap())))
        .unwrap_or_else(|_| json!({"ok":false,"error":{"code":"dispatcher_panic","message":"dispatcher panicked; request was not retried"}}))
}

fn inspect(dispatch: &impl Fn(&[u8]) -> Value, host: u64) -> Value {
    dispatch_call(
        dispatch,
        &json!({"version":1,"command":"runtime_inspect","protocol":"plugin-runtime/1","host":host}),
    )
}

fn exchange(
    worker: &mut Worker,
    dispatch: &impl Fn(&[u8]) -> Value,
    roots: &mut BTreeSet<u64>,
    id: u64,
    method: &str,
    params: Value,
    deadline: Option<Instant>,
) -> Result<Value, HostError> {
    worker.send(
        json!({"protocol":PROTOCOL,"kind":"request","id":id,"method":method,"params":params}),
    )?;
    match next_response(worker, dispatch, roots, deadline)? {
        Some((response_id, result)) if response_id == id => Ok(result),
        _ => Err(HostError::new(
            "protocol",
            "unexpected lifecycle response ID",
        )),
    }
}

fn next_response(
    worker: &mut Worker,
    dispatch: &impl Fn(&[u8]) -> Value,
    roots: &mut BTreeSet<u64>,
    deadline: Option<Instant>,
) -> Result<Option<(u64, Value)>, HostError> {
    loop {
        let value = match worker.event(deadline)? {
            Event::Frame(value) => value,
            Event::Failed(message) => return Err(HostError::new("transport", message)),
            Event::Eof => return Err(HostError::new("worker_exit", "unexpected worker EOF")),
            Event::Written(_) => unreachable!(),
            Event::Stop => return Ok(None),
        };
        if value["protocol"] != PROTOCOL || value["id"].as_u64().is_none() {
            return Err(HostError::new("protocol", "invalid protocol or request ID"));
        }
        match value["kind"].as_str() {
            Some("request") => {
                if value["method"] != "native" || !value["params"].is_object() {
                    return Err(HostError::new(
                        "protocol",
                        "worker requests must use native with object params",
                    ));
                }
                let request = &value["params"];
                let response = dispatch_call(dispatch, request);
                if response["ok"] == true {
                    if request["command"] == "runtime_create" && request["parent"].is_null() {
                        let root = response["result"]["host"].as_u64().ok_or_else(|| {
                            HostError::new("dispatcher", "runtime_create returned no host identity")
                        })?;
                        roots.insert(root);
                    } else if request["command"] == "runtime_collect"
                        && let Some(root) = request["host"].as_u64()
                    {
                        roots.remove(&root);
                    }
                }
                worker.send(json!({"protocol":PROTOCOL,"kind":"response","id":value["id"],"ok":true,"result":response}))?;
            }
            Some("response") => {
                if value["ok"] == true && value.get("result").is_some() {
                    return Ok(Some((
                        value["id"].as_u64().unwrap(),
                        value["result"].clone(),
                    )));
                }
                if value["ok"] == false
                    && value["error"]["code"].is_string()
                    && value["error"]["message"].is_string()
                {
                    return Err(HostError::new(
                        "application",
                        format!(
                            "request {}: {}: {}",
                            value["id"], value["error"]["code"], value["error"]["message"]
                        ),
                    ));
                }
                return Err(HostError::new("protocol", "invalid response outcome"));
            }
            _ => {
                return Err(HostError::new(
                    "protocol",
                    "unexpected frame kind or response ID",
                ));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn bounded_frames_require_delimiters_and_accept_crlf() {
        let mut input = BufReader::with_capacity(3, Cursor::new(b"{}\r\n[]\n"));
        assert_eq!(read_frame(&mut input).unwrap().unwrap(), b"{}\r");
        assert_eq!(read_frame(&mut input).unwrap().unwrap(), b"[]");
        assert!(read_frame(&mut input).unwrap().is_none());
        assert!(read_frame(&mut Cursor::new(b"{}")).is_err());
        assert!(read_frame(&mut Cursor::new(vec![b'x'; MAX_FRAME_BYTES + 1])).is_err());
    }

    #[test]
    fn dispatcher_preserves_raw_failure_envelopes() {
        let response = dispatch_call(
            &|_| json!({"ok":false,"error":{"code":"expected"}}),
            &json!({}),
        );
        assert_eq!(response["error"]["code"], "expected");
    }
}
