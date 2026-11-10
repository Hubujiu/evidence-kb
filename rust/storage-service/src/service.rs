use crate::{
    StorageError,
    contracts::{Contract, StorageIdentity},
    fields,
    operations::{Operation, catalogue},
    values::{CodecLimits, StorageValue, ValueCodec, ValueInfo},
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Descriptor {
    pub identity: StorageIdentity,
    pub contract: Contract,
    pub methods: Vec<String>,
}

impl Descriptor {
    pub fn parse(value: &Value) -> Result<Self, StorageError> {
        crate::contracts::validate_descriptor(value)?;
        serde_json::from_value(value.clone())
            .map_err(|_| StorageError::new("STORAGE_IMPLEMENTATION_INVALID"))
    }
    pub fn operation(&self, method: &str) -> Result<Operation, StorageError> {
        if !self.methods.iter().any(|name| name == method) {
            return Err(StorageError::new("STORAGE_PROCESS_METHOD_REJECTED"));
        }
        catalogue(&self.contract)
            .remove(method)
            .ok_or_else(|| StorageError::new("STORAGE_PROCESS_METHOD_REJECTED"))
    }
    pub fn describe(&self) -> Value {
        json!({"identity":self.identity,"contract":self.contract,"methods":self.methods,"operations":catalogue(&self.contract)})
    }
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallLimits {
    pub request: CodecLimits,
    pub response: CodecLimits,
}

pub struct PreparedOperation {
    pub operation: Operation,
    bound: Vec<usize>,
    limits: CallLimits,
}

impl PreparedOperation {
    pub fn new(
        descriptor: &Descriptor,
        method: &str,
        argc: usize,
        keywords: &[String],
        limits: CallLimits,
    ) -> Result<Self, StorageError> {
        limits.request.validate()?;
        limits.response.validate()?;
        let operation = descriptor.operation(method)?;
        let bound = operation.bind(argc, keywords)?;
        Ok(Self {
            operation,
            bound,
            limits,
        })
    }
    pub fn admit(&self, inputs: &[ValueInfo]) -> Result<(), StorageError> {
        self.operation
            .validate_input(&self.bound, inputs, self.limits.response.max_bytes)
    }
    pub fn complete(&self, result: Result<ValueInfo, StorageError>) -> Result<(), StorageError> {
        result
            .and_then(|value| self.operation.validate_result(&value))
            .map_err(|error| self.operation.result_failure(error))
    }
}

pub trait Driver {
    fn descriptor(&self) -> Descriptor;
    fn invoke(
        &mut self,
        method: &str,
        args: Vec<StorageValue>,
        kwargs: BTreeMap<String, StorageValue>,
    ) -> Result<StorageValue, StorageError>;
}

pub struct Service<D: Driver> {
    driver: D,
    descriptor: Descriptor,
}

impl<D: Driver> Service<D> {
    pub fn new(driver: D) -> Result<Self, StorageError> {
        let descriptor =
            Descriptor::parse(&serde_json::to_value(driver.descriptor()).expect("descriptor"))?;
        Ok(Self { driver, descriptor })
    }
    pub fn descriptor(&self) -> &Descriptor {
        &self.descriptor
    }
    pub fn invoke(
        &mut self,
        method: &str,
        args: Vec<StorageValue>,
        kwargs: BTreeMap<String, StorageValue>,
    ) -> Result<StorageValue, StorageError> {
        if self.driver.descriptor() != self.descriptor {
            return Err(StorageError::new("STORAGE_BINDING_CHANGED"));
        }
        let limits = CallLimits::default();
        let prepared = PreparedOperation::new(
            &self.descriptor,
            method,
            args.len(),
            &kwargs.keys().cloned().collect::<Vec<_>>(),
            limits,
        )?;
        let mut codec = ValueCodec::new(limits.request)?;
        for value in args.iter().chain(kwargs.values()) {
            value.visit(&mut codec, 0)?;
        }
        prepared.admit(&codec.finish()?)?;
        let result = self.driver.invoke(method, args, kwargs)?;
        prepared.complete(result.inspect(limits.response))?;
        Ok(result)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Input,
    Executing,
    Result,
    Values,
    Failed,
}

struct Session {
    operation: Option<PreparedOperation>,
    codec: Option<ValueCodec>,
    phase: Phase,
    touched: Instant,
}

impl Session {
    fn take_values(&mut self) -> Result<Vec<ValueInfo>, StorageError> {
        self.codec.take().ok_or_else(state_error)?.finish()
    }
    fn failure(&self, error: StorageError) -> StorageError {
        if self.phase == Phase::Result {
            self.operation
                .as_ref()
                .unwrap()
                .operation
                .result_failure(error)
        } else {
            error
        }
    }
}

#[derive(Default)]
struct Sessions {
    next: u64,
    values: BTreeMap<u64, Session>,
}
static SESSIONS: OnceLock<Mutex<Sessions>> = OnceLock::new();
fn state_error() -> StorageError {
    StorageError::new("STORAGE_CALL_STATE_INVALID")
}
fn parse<T: serde::de::DeserializeOwned>(value: &Value) -> Result<T, StorageError> {
    serde_json::from_value(value.clone())
        .map_err(|_| StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"))
}

pub fn dispatch(arguments: &Map<String, Value>) -> Result<Value, StorageError> {
    let action = arguments
        .get("action")
        .and_then(Value::as_str)
        .ok_or_else(state_error)?;
    if action == "describe" {
        fields(
            arguments,
            &["action", "descriptor"],
            &[],
            "STORAGE_INVALID_PROCESS_REQUEST",
        )?;
        return Ok(Descriptor::parse(&arguments["descriptor"])?.describe());
    }
    if action == "result_error" {
        fields(
            arguments,
            &["action", "descriptor", "method", "code"],
            &[],
            "STORAGE_INVALID_PROCESS_REQUEST",
        )?;
        let descriptor = Descriptor::parse(&arguments["descriptor"])?;
        let method = arguments["method"].as_str().ok_or_else(state_error)?;
        let code = arguments["code"].as_str().ok_or_else(state_error)?;
        return Err(descriptor
            .operation(method)?
            .result_failure(StorageError::new(code)));
    }
    let mut sessions = SESSIONS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| state_error())?;
    sessions
        .values
        .retain(|_, session| session.touched.elapsed() < Duration::from_secs(300));
    if action == "open" || action == "values" {
        let (operation, limits, phase) = if action == "open" {
            fields(
                arguments,
                &[
                    "action",
                    "descriptor",
                    "method",
                    "argc",
                    "keywords",
                    "limits",
                ],
                &[],
                "STORAGE_INVALID_PROCESS_REQUEST",
            )?;
            let descriptor = Descriptor::parse(&arguments["descriptor"])?;
            let method = arguments["method"].as_str().ok_or_else(state_error)?;
            let argc = arguments["argc"].as_u64().ok_or_else(state_error)? as usize;
            let keywords: Vec<String> = parse(&arguments["keywords"])?;
            let limits: CallLimits = parse(&arguments["limits"])?;
            (
                Some(PreparedOperation::new(
                    &descriptor,
                    method,
                    argc,
                    &keywords,
                    limits,
                )?),
                limits.request,
                Phase::Input,
            )
        } else {
            fields(
                arguments,
                &["action", "limits"],
                &[],
                "STORAGE_INVALID_PROCESS_REQUEST",
            )?;
            (None, parse(&arguments["limits"])?, Phase::Values)
        };
        if sessions.values.len() >= 64 {
            return Err(StorageError::new("STORAGE_CODEC_BUSY"));
        }
        let codec = ValueCodec::new(limits)?;
        sessions.next = sessions.next.checked_add(1).ok_or_else(state_error)?;
        let handle = sessions.next;
        sessions.values.insert(
            handle,
            Session {
                operation,
                codec: Some(codec),
                phase,
                touched: Instant::now(),
            },
        );
        return Ok(json!({"handle":handle}));
    }
    let handle = arguments
        .get("handle")
        .and_then(Value::as_u64)
        .ok_or_else(state_error)?;
    if action == "close" {
        fields(
            arguments,
            &["action", "handle"],
            &[],
            "STORAGE_INVALID_PROCESS_REQUEST",
        )?;
        sessions.values.remove(&handle);
        return Ok(json!({"closed":true}));
    }
    if action == "finish" {
        fields(
            arguments,
            &["action", "handle"],
            &[],
            "STORAGE_INVALID_PROCESS_REQUEST",
        )?;
        let mut session = sessions.values.remove(&handle).ok_or_else(state_error)?;
        let values = session.take_values();
        if session.phase == Phase::Values {
            return Ok(json!({"count":values?.len()}));
        }
        if session.phase != Phase::Result {
            return Err(state_error());
        }
        let result = values.and_then(|mut values| {
            if values.len() == 1 {
                Ok(values.remove(0))
            } else {
                Err(state_error())
            }
        });
        session.operation.as_ref().unwrap().complete(result)?;
        return Ok(json!({"completed":true}));
    }
    let session = sessions.values.get_mut(&handle).ok_or_else(state_error)?;
    session.touched = Instant::now();
    let result = (|| match action {
        "feed" => {
            fields(
                arguments,
                &["action", "handle", "events"],
                &[],
                "STORAGE_INVALID_PROCESS_REQUEST",
            )?;
            let events = arguments["events"]
                .as_array()
                .filter(|values| values.len() <= 2048)
                .ok_or_else(state_error)?;
            let codec = session.codec.as_mut().ok_or_else(state_error)?;
            for event in events {
                codec.push(event)?;
            }
            Ok(json!({"accepted":events.len()}))
        }
        "admit" => {
            fields(
                arguments,
                &["action", "handle"],
                &[],
                "STORAGE_INVALID_PROCESS_REQUEST",
            )?;
            if session.phase != Phase::Input {
                return Err(state_error());
            }
            let values = session.take_values()?;
            let prepared = session.operation.as_ref().unwrap();
            prepared.admit(&values)?;
            session.phase = Phase::Executing;
            Ok(json!({"operation":prepared.operation}))
        }
        "result" => {
            fields(
                arguments,
                &["action", "handle"],
                &[],
                "STORAGE_INVALID_PROCESS_REQUEST",
            )?;
            if session.phase != Phase::Executing {
                return Err(state_error());
            }
            session.codec = Some(ValueCodec::new(
                session.operation.as_ref().unwrap().limits.response,
            )?);
            session.phase = Phase::Result;
            Ok(json!({"ready":true}))
        }
        _ => Err(state_error()),
    })();
    result.map_err(|error| {
        let error = session.failure(error);
        session.codec = None;
        session.phase = Phase::Failed;
        error
    })
}

pub fn reply(arguments: &Map<String, Value>) -> Value {
    match dispatch(arguments) {
        Ok(value) => json!({"ok":true,"result":value}),
        Err(error) => json!({"ok":false,"error":error}),
    }
}

#[cfg(test)]
#[path = "session_contracts.rs"]
mod session_contracts;
