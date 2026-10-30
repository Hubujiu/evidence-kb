use crate::Manifest;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Pending,
    Starting,
    Active,
    Draining,
    Cleaning,
    CleanupFailed,
    Failed,
    Disposed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Desired {
    Enabled,
    Disabled,
    Removed,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Binding {
    pub runtime: u64,
    pub host: u64,
    pub plugin: String,
    pub instance: u64,
    pub generation: u64,
    pub registration: u64,
    pub service: String,
    pub definition_scope: u64,
    pub call_scope: u64,
    pub intercept: Value,
}

#[derive(Clone, Debug, Serialize)]
pub struct Action {
    pub kind: String,
    pub runtime: u64,
    pub host: u64,
    pub instance: u64,
    pub plugin: String,
    pub generation: u64,
    pub operation: u64,
    pub config: Value,
    pub bindings: BTreeMap<String, Binding>,
    pub resource: Option<u64>,
    pub deadline: Option<u64>,
    pub cancelled: bool,
    pub lease: Option<u64>,
    pub method: Option<String>,
    pub implementation: String,
    pub binding: Option<Binding>,
    pub configuration_inputs: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Completion {
    pub status: String,
    #[serde(default)]
    pub services: Vec<String>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub result: Value,
}

#[derive(Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    RuntimeValidate {
        plugins: Vec<Manifest>,
        #[serde(default)]
        parent: Option<u64>,
        #[serde(default)]
        isolated: Vec<String>,
        #[serde(default = "empty_object")]
        intercept: Value,
    },
    RuntimeCreate {
        plugins: Vec<Manifest>,
        #[serde(default)]
        parent: Option<u64>,
        #[serde(default)]
        isolated: Vec<String>,
        #[serde(default = "empty_object")]
        intercept: Value,
    },
    RuntimePoll {
        host: u64,
        #[serde(default = "default_limit")]
        limit: usize,
    },
    RuntimeComplete {
        host: u64,
        operation: u64,
        #[serde(flatten)]
        completion: Completion,
    },
    RuntimeResource {
        host: u64,
        operation: u64,
        kind: String,
    },
    RuntimeResolve {
        host: u64,
        service: String,
        #[serde(default)]
        consumer: Option<u64>,
        #[serde(default)]
        cleanup: Option<u64>,
        #[serde(default)]
        parent_lease: Option<u64>,
        #[serde(default)]
        consumer_generation: Option<u64>,
    },
    RuntimeAcquire {
        host: u64,
        #[serde(default)]
        registration: Option<u64>,
        #[serde(default)]
        instance: Option<u64>,
        #[serde(default)]
        generation: Option<u64>,
        #[serde(default)]
        parent_lease: Option<u64>,
        #[serde(default)]
        deadline: Option<u64>,
    },
    RuntimeInvoke {
        host: u64,
        registration: u64,
        method: String,
        #[serde(default)]
        call: Option<u64>,
        #[serde(default)]
        parent_lease: Option<u64>,
        #[serde(default)]
        deadline: Option<u64>,
    },
    RuntimeRelease {
        host: u64,
        lease: u64,
    },
    RuntimeDrain {
        host: u64,
        #[serde(default = "default_remove")]
        remove: bool,
    },
    RuntimeUpdate {
        host: u64,
        plugin: String,
        #[serde(default)]
        enabled: Option<bool>,
        #[serde(default)]
        remove: bool,
        #[serde(default)]
        config: Option<Value>,
        #[serde(default)]
        manifest: Option<Box<Manifest>>,
        #[serde(default)]
        configuration_inputs: Option<BTreeMap<String, String>>,
    },
    RuntimeRetry {
        host: u64,
        #[serde(default)]
        plugin: Option<String>,
    },
    RuntimeInspect {
        host: u64,
    },
    RuntimeCollect {
        host: u64,
    },
    RuntimeAck {
        host: u64,
        operations: Vec<u64>,
    },
    RuntimeDiagnostic {
        host: u64,
        code: String,
        message: String,
        #[serde(default)]
        instance: Option<u64>,
        #[serde(default)]
        operation: Option<u64>,
    },
    RuntimeCancel {
        host: u64,
        operation: u64,
    },
    RuntimeTick {
        host: u64,
        now: u64,
    },
    RuntimeListener {
        host: u64,
        operation: u64,
        event: String,
        #[serde(default)]
        priority: i32,
    },
    RuntimeListeners {
        host: u64,
        event: String,
    },
    RuntimeApply {
        host: u64,
        plugins: Vec<Manifest>,
    },
}

fn default_limit() -> usize {
    64
}

fn empty_object() -> Value {
    serde_json::json!({})
}
fn default_remove() -> bool {
    true
}
