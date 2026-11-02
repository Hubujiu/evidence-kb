use crate::wire::{LegacyRequest, LegacyResponse, normalize_legacy, validate_legacy};
use crate::{
    Admission, Binding, LlmError, Prepared, RegistrationIdentity, Registry, RouteSnapshot,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{
    Arc, Mutex, OnceLock,
    atomic::{AtomicU64, Ordering},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelDescriptor {
    provider: String,
    model: String,
    capabilities: BTreeSet<String>,
    input_modalities: BTreeSet<String>,
    streaming: bool,
    revision: String,
    preferred_batch_size: u64,
    protocol: String,
}

impl ModelDescriptor {
    fn validate(&self) -> Result<(), LlmError> {
        if self.provider.is_empty()
            || self.model.is_empty()
            || self.revision.is_empty()
            || self.preferred_batch_size == 0
            || self.protocol != "model-service/1"
            || self.capabilities.is_empty()
            || self.capabilities.iter().any(|capability| {
                !matches!(
                    capability.as_str(),
                    "chat" | "embedding" | "ocr" | "transcription" | "speech" | "image"
                )
            })
            || self.input_modalities.is_empty()
            || self
                .input_modalities
                .iter()
                .any(|modality| !matches!(modality.as_str(), "text" | "image" | "audio"))
        {
            return Err(LlmError::new(
                "INVALID_LLM_MODEL_INFO",
                "invalid external model descriptor",
            ));
        }
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Adoption {
    registration: u64,
    identity: RegistrationIdentity,
}

#[derive(Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
enum CallKind {
    Ownership,
    Invoke,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Completion {
    provider: String,
    model: String,
    capability: String,
    text_is_text: Option<bool>,
    finish_reason: Option<String>,
    usage: Option<Value>,
}

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
    ServiceCreate,
    ServiceInspect {
        service: u64,
    },
    ServiceBeginClose {
        service: u64,
    },
    ServiceFinishClose {
        service: u64,
    },
    RouteRegister {
        service: u64,
        name: String,
        info: ModelDescriptor,
        identity: Option<RegistrationIdentity>,
    },
    RouteBind {
        service: u64,
        name: String,
        capability: String,
        expected: Option<RegistrationIdentity>,
    },
    RouteAdopt {
        service: u64,
        identities: Vec<Adoption>,
    },
    RouteBeginCleanup {
        service: u64,
        name: String,
        binding: Binding,
    },
    RouteFinishCleanup {
        service: u64,
        binding: Binding,
        error: Option<String>,
    },
    CallPrepare {
        service: u64,
        name: String,
        capability: String,
        expected: Option<RegistrationIdentity>,
        kind: CallKind,
        request: Option<LegacyRequest>,
    },
    CallBegin {
        service: u64,
        prepared: u64,
    },
    CallDiscard {
        service: u64,
        prepared: u64,
    },
    CallComplete {
        service: u64,
        call: u64,
        result: Completion,
    },
    CallRelease {
        service: u64,
        call: u64,
    },
}

struct ExternalPrepared {
    prepared: Prepared<ModelDescriptor>,
    capability: String,
    kind: CallKind,
}
struct ExternalCall {
    admission: Admission<ModelDescriptor>,
    capability: String,
    kind: CallKind,
    completed: bool,
}

#[derive(Default)]
struct ExternalService {
    registry: Registry<ModelDescriptor>,
    prepared: BTreeMap<u64, ExternalPrepared>,
    calls: BTreeMap<u64, ExternalCall>,
}

impl ExternalService {
    fn ticket(&self) -> Result<u64, LlmError> {
        static TICKETS: AtomicU64 = AtomicU64::new(1);
        let mut current = TICKETS.load(Ordering::SeqCst);
        loop {
            let next = current.checked_add(1).ok_or_else(|| {
                LlmError::new("LLM_GENERATION_EXHAUSTED", "call tickets exhausted")
            })?;
            match TICKETS.compare_exchange_weak(current, next, Ordering::SeqCst, Ordering::SeqCst) {
                Ok(_) => return Ok(current),
                Err(actual) => current = actual,
            }
        }
    }

    fn prepare(
        &self,
        name: &str,
        capability: &str,
        expected: Option<&RegistrationIdentity>,
    ) -> Result<Prepared<ModelDescriptor>, LlmError> {
        let prepared = self.registry.prepare(name, expected)?;
        if !prepared
            .snapshot()
            .payload
            .capabilities
            .contains(capability)
        {
            return Err(LlmError::new("LLM_CAPABILITY_UNAVAILABLE", capability));
        }
        Ok(prepared)
    }
}

fn row(snapshot: &RouteSnapshot<ModelDescriptor>) -> Value {
    json!({"name": snapshot.name, "binding": snapshot.binding, "identity": snapshot.identity,
        "phase": snapshot.phase, "activeCalls": snapshot.active, "cleanupError": snapshot.cleanup_error,
        "info": snapshot.payload.as_ref()})
}

fn rows(snapshots: Vec<RouteSnapshot<ModelDescriptor>>) -> Value {
    Value::Array(snapshots.iter().map(row).collect())
}

pub(crate) fn dispatch(operation: &str, mut object: Map<String, Value>) -> Result<Value, LlmError> {
    object.insert("operation".into(), Value::String(operation.into()));
    let command: Command = serde_json::from_value(Value::Object(object))
        .map_err(|error| LlmError::new("INVALID_LLM_SERVICE_COMMAND", error.to_string()))?;
    static SERVICES: OnceLock<Mutex<BTreeMap<u64, ExternalService>>> = OnceLock::new();
    let mut services = SERVICES
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| LlmError::new("LLM_SERVICE_UNAVAILABLE", "executor service lock poisoned"))?;
    let service_id = match &command {
        Command::ServiceCreate => {
            let service = ExternalService::default();
            let id = service.registry.identity();
            services.insert(id, service);
            return Ok(json!({"service": id}));
        }
        Command::ServiceInspect { service }
        | Command::ServiceBeginClose { service }
        | Command::ServiceFinishClose { service }
        | Command::RouteRegister { service, .. }
        | Command::RouteBind { service, .. }
        | Command::RouteAdopt { service, .. }
        | Command::RouteBeginCleanup { service, .. }
        | Command::RouteFinishCleanup { service, .. }
        | Command::CallPrepare { service, .. }
        | Command::CallBegin { service, .. }
        | Command::CallDiscard { service, .. }
        | Command::CallComplete { service, .. }
        | Command::CallRelease { service, .. } => *service,
    };
    let service = services
        .get_mut(&service_id)
        .ok_or_else(|| LlmError::new("LLM_SERVICE_UNAVAILABLE", "unknown executor service"))?;
    match command {
        Command::ServiceCreate => unreachable!(),
        Command::ServiceInspect { .. } => Ok(json!({"routes": rows(service.registry.inspect()?),
            "prepared": service.prepared.len(), "calls": service.calls.len()})),
        Command::ServiceBeginClose { .. } => {
            let snapshots = service.registry.begin_close()?;
            service.prepared.clear();
            Ok(json!({"routes": rows(snapshots)}))
        }
        Command::ServiceFinishClose { .. } => {
            service.registry.finish_close()?;
            services.remove(&service_id);
            Ok(json!({}))
        }
        Command::RouteRegister {
            name,
            info,
            identity,
            ..
        } => {
            info.validate()?;
            let binding = service.registry.register(&name, Arc::new(info), identity)?;
            let prepared = service.registry.prepare(&name, None)?;
            Ok(json!({"binding": binding, "route": row(prepared.snapshot())}))
        }
        Command::RouteBind {
            name,
            capability,
            expected,
            ..
        } => Ok(row(service
            .prepare(&name, &capability, expected.as_ref())?
            .snapshot())),
        Command::RouteAdopt { identities, .. } => {
            let snapshots = service.registry.adopt(
                identities
                    .into_iter()
                    .map(|identity| (identity.registration, identity.identity))
                    .collect(),
            )?;
            service.prepared.clear();
            Ok(json!({"routes": rows(snapshots)}))
        }
        Command::RouteBeginCleanup { name, binding, .. } => {
            Ok(json!({"route": service.registry.begin_cleanup(&name, binding)?.as_ref().map(row)}))
        }
        Command::RouteFinishCleanup { binding, error, .. } => {
            service.registry.finish_cleanup(binding, error)?;
            Ok(json!({}))
        }
        Command::CallPrepare {
            name,
            capability,
            expected,
            kind,
            request,
            ..
        } => {
            let prepared = service.prepare(&name, &capability, expected.as_ref())?;
            if kind == CallKind::Invoke && capability == "chat" {
                let request = request.ok_or_else(|| {
                    LlmError::new(
                        "INVALID_LLM_REQUEST",
                        "chat attempt requires request metadata",
                    )
                })?;
                let info = &prepared.snapshot().payload;
                if request.provider != info.provider || request.model != info.model {
                    return Err(LlmError::new(
                        "LLM_PREPARED_ROUTE_MISMATCH",
                        "request metadata does not match the registered model",
                    ));
                }
                validate_legacy(&request)?;
            } else if request.is_some() {
                return Err(LlmError::new(
                    "INVALID_LLM_REQUEST",
                    "unexpected request metadata",
                ));
            }
            let ticket = service.ticket()?;
            let result = json!({"prepared": ticket, "route": row(prepared.snapshot())});
            service.prepared.insert(
                ticket,
                ExternalPrepared {
                    prepared,
                    capability,
                    kind,
                },
            );
            Ok(result)
        }
        Command::CallBegin { prepared, .. } => {
            let prepared = service.prepared.remove(&prepared).ok_or_else(|| {
                LlmError::new(
                    "LLM_PREPARED_UNAVAILABLE",
                    "prepared call was consumed or discarded",
                )
            })?;
            let ticket = service.ticket()?;
            let admission = prepared.prepared.admit()?;
            let result = json!({"call": ticket, "route": row(admission.snapshot())});
            service.calls.insert(
                ticket,
                ExternalCall {
                    admission,
                    capability: prepared.capability,
                    kind: prepared.kind,
                    completed: false,
                },
            );
            Ok(result)
        }
        Command::CallDiscard { prepared, .. } => {
            service.prepared.remove(&prepared).ok_or_else(|| {
                LlmError::new(
                    "LLM_PREPARED_UNAVAILABLE",
                    "prepared call was consumed or discarded",
                )
            })?;
            Ok(json!({}))
        }
        Command::CallComplete { call, result, .. } => {
            let call = service
                .calls
                .get_mut(&call)
                .ok_or_else(|| LlmError::new("LLM_CALL_UNAVAILABLE", "unknown or released call"))?;
            if call.kind != CallKind::Invoke || call.completed {
                return Err(LlmError::new(
                    "LLM_CALL_FINISHED",
                    "call cannot accept another result",
                ));
            }
            let info = &call.admission.snapshot().payload;
            if result.provider != info.provider
                || result.model != info.model
                || result.capability != call.capability
            {
                return Err(LlmError::new(
                    "INVALID_LLM_RESPONSE",
                    "result does not match its admitted model call",
                ));
            }
            let normalized = if call.capability == "chat" {
                normalize_legacy(LegacyResponse {
                    provider: result.provider,
                    model: result.model,
                    expected_provider: info.provider.clone(),
                    expected_model: info.model.clone(),
                    text_is_text: result.text_is_text == Some(true),
                    finish_reason: result.finish_reason,
                    usage: result.usage,
                })?
            } else {
                json!({})
            };
            call.completed = true;
            Ok(normalized)
        }
        Command::CallRelease { call, .. } => {
            service
                .calls
                .remove(&call)
                .ok_or_else(|| LlmError::new("LLM_CALL_UNAVAILABLE", "unknown or released call"))?;
            Ok(json!({}))
        }
    }
}
