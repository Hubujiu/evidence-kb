//! Loop policies emit service effects; the session service is the sole history owner.

use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::Error;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoopConfig {
    pub mode: String,
    pub message: Value,
    pub tools: Vec<Value>,
    pub tool_name: Option<String>,
    pub arguments: Option<serde_json::Map<String, Value>>,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Effect {
    pub effect_id: u64,
    pub name: String,
    pub arguments: Value,
}

#[derive(Debug, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Instruction {
    Host(Effect),
    Model(Effect),
    Tool(Effect),
    Complete { value: Value },
}

pub trait LoopProgram: Send {
    fn next(&mut self) -> Result<Instruction, Error>;
    fn accept(&mut self, effect_id: u64, result: Value) -> Result<(), Error>;
    fn snapshot(&self) -> Value;
}

pub type LoopFactory = fn(LoopConfig) -> Result<Box<dyn LoopProgram>, Error>;

pub struct LoopRegistry {
    factories: BTreeMap<String, LoopFactory>,
    programs: BTreeMap<String, Box<dyn LoopProgram>>,
    sequence: u64,
}

impl Default for LoopRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl LoopRegistry {
    pub fn new() -> Self {
        Self {
            factories: BTreeMap::new(),
            programs: BTreeMap::new(),
            sequence: 0,
        }
    }

    pub fn with_builtins() -> Self {
        let mut registry = Self::new();
        registry.factories.insert("model-tools".into(), model_tools);
        registry.factories.insert("single-tool".into(), single_tool);
        registry
    }

    pub fn register(&mut self, name: &str, factory: LoopFactory) -> Result<(), Error> {
        if name.is_empty() || name.len() > 128 || self.factories.contains_key(name) {
            return Err(Error::new(
                "INVALID_LOOP_PLUGIN",
                "loop name is empty or already registered",
            ));
        }
        self.factories.insert(name.into(), factory);
        Ok(())
    }

    pub fn create(&mut self, config: LoopConfig) -> Result<Value, Error> {
        if self.programs.len() >= 4096 {
            return Err(Error::new(
                "LOOP_CAPACITY",
                "close unused loops before creating more",
            ));
        }
        let factory = self.factories.get(&config.mode).ok_or_else(|| {
            Error::new(
                "UNKNOWN_LOOP_PLUGIN",
                format!("loop plugin {} is not registered", config.mode),
            )
        })?;
        let program = factory(config)?;
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| Error::new("LOOP_ID_EXHAUSTED", "loop identity space exhausted"))?;
        let id = format!("loop-{}", self.sequence);
        self.programs.insert(id.clone(), program);
        Ok(json!({"loop_id": id}))
    }

    pub fn next(&mut self, id: &str) -> Result<Instruction, Error> {
        let result = self.program_mut(id)?.next();
        if result.is_err() {
            self.programs.remove(id);
        }
        result
    }

    pub fn accept(&mut self, id: &str, effect_id: u64, result: Value) -> Result<Value, Error> {
        if let Err(error) = self.program_mut(id)?.accept(effect_id, result) {
            self.programs.remove(id);
            return Err(error);
        }
        self.snapshot(id)
    }

    pub fn snapshot(&self, id: &str) -> Result<Value, Error> {
        self.programs
            .get(id)
            .map(|program| program.snapshot())
            .ok_or_else(|| Error::new("LOOP_NOT_FOUND", "loop has closed or does not exist"))
    }

    pub fn close(&mut self, id: &str) -> Value {
        json!({"closed": self.programs.remove(id).is_some()})
    }

    fn program_mut(&mut self, id: &str) -> Result<&mut Box<dyn LoopProgram>, Error> {
        self.programs
            .get_mut(id)
            .ok_or_else(|| Error::new("LOOP_NOT_FOUND", "loop has closed or does not exist"))
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolCall {
    name: String,
    arguments: serde_json::Map<String, Value>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Decision {
    Answer {
        value: Value,
    },
    Tool {
        name: String,
        arguments: serde_json::Map<String, Value>,
    },
}

enum Continuation {
    Context,
    Tool(ToolCall),
    Complete(Value),
}

enum Phase {
    Start(Value),
    Context,
    Model(Value),
    Append {
        records: Vec<Value>,
        then: Continuation,
    },
    Tool(ToolCall),
    Complete(Value),
    Failed,
}

struct ServiceLoop {
    phase: Phase,
    tools: BTreeMap<String, bool>,
    first_tool: Option<ToolCall>,
    direct_mode: bool,
    pending: Option<u64>,
    sequence: u64,
    tool_count: u64,
}

impl ServiceLoop {
    fn create(config: LoopConfig, direct_mode: bool) -> Result<Self, Error> {
        let mut tools = BTreeMap::new();
        for tool in config.tools {
            let name = tool
                .get("name")
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty() && name.len() <= 128)
                .ok_or_else(|| {
                    Error::new("INVALID_TOOL_MANIFEST", "tool requires a nonempty name")
                })?;
            let direct = match tool.get("direct_return") {
                None => false,
                Some(Value::Bool(value)) => *value,
                _ => {
                    return Err(Error::new(
                        "INVALID_TOOL_MANIFEST",
                        "direct_return must be boolean",
                    ));
                }
            };
            if tools.insert(name.into(), direct).is_some() {
                return Err(Error::new(
                    "DUPLICATE_TOOL",
                    "tool manifest names must be unique",
                ));
            }
        }
        let first_tool = if direct_mode {
            let name = config.tool_name.ok_or_else(|| {
                Error::new(
                    "INVALID_LOOP_CONFIG",
                    "single-tool requires a trusted tool_name",
                )
            })?;
            if !tools.contains_key(&name) {
                return Err(Error::new(
                    "UNKNOWN_TOOL",
                    "single-tool target is not in the tool manifest",
                ));
            }
            let arguments = config.arguments.ok_or_else(|| {
                Error::new(
                    "INVALID_LOOP_CONFIG",
                    "single-tool requires explicit arguments",
                )
            })?;
            Some(ToolCall { name, arguments })
        } else {
            if config.tool_name.is_some() || config.arguments.is_some() {
                return Err(Error::new(
                    "INVALID_LOOP_CONFIG",
                    "model-tools does not accept a fixed tool",
                ));
            }
            None
        };
        Ok(Self {
            phase: Phase::Start(config.message),
            tools,
            first_tool,
            direct_mode,
            pending: None,
            sequence: 0,
            tool_count: 0,
        })
    }

    fn record_call(call: ToolCall) -> Phase {
        Phase::Append {
            records: vec![json!({"role": "assistant", "tool_call": {
                "name": call.name, "arguments": call.arguments,
            }})],
            then: Continuation::Tool(call),
        }
    }

    fn accept_phase(&mut self, phase: Phase, result: Value) -> Result<Phase, Error> {
        Ok(match phase {
            Phase::Start(_) => {
                let session = result.as_object().ok_or_else(|| {
                    Error::new(
                        "INVALID_SESSION_RESULT",
                        "session.start must return an object",
                    )
                })?;
                if let Some(cached) = session.get("cached") {
                    Phase::Complete(cached.clone())
                } else {
                    match self.first_tool.take() {
                        Some(call) => Self::record_call(call),
                        None => Phase::Context,
                    }
                }
            }
            Phase::Context => {
                if !result.is_object()
                    || !result.get("messages").is_some_and(Value::is_array)
                    || !result.get("tools").is_some_and(Value::is_array)
                {
                    return Err(Error::new(
                        "INVALID_CONTEXT",
                        "context service must return messages and tools arrays",
                    ));
                }
                Phase::Model(result)
            }
            Phase::Model(_) => match serde_json::from_value::<Decision>(result)
                .map_err(|error| Error::new("INVALID_MODEL_DECISION", error.to_string()))?
            {
                Decision::Answer { value } => Phase::Append {
                    records: vec![json!({"role": "assistant", "content": value})],
                    then: Continuation::Complete(value),
                },
                Decision::Tool { name, arguments } => {
                    if !self.tools.contains_key(&name) {
                        return Err(Error::new(
                            "UNKNOWN_TOOL",
                            "model selected an unavailable tool",
                        ));
                    }
                    Self::record_call(ToolCall { name, arguments })
                }
            },
            Phase::Append { then, .. } => match then {
                Continuation::Context => Phase::Context,
                Continuation::Tool(call) => Phase::Tool(call),
                Continuation::Complete(value) => Phase::Complete(value),
            },
            Phase::Tool(call) => {
                self.tool_count += 1;
                let mut records =
                    vec![json!({"role": "tool", "name": call.name, "content": result})];
                let then = if self.direct_mode || self.tools[&call.name] {
                    records.push(json!({"role": "assistant", "content": result}));
                    Continuation::Complete(result)
                } else {
                    Continuation::Context
                };
                Phase::Append { records, then }
            }
            Phase::Complete(_) | Phase::Failed => {
                return Err(Error::new(
                    "LOOP_TERMINAL",
                    "terminal loops cannot accept results",
                ));
            }
        })
    }
}

impl LoopProgram for ServiceLoop {
    fn next(&mut self) -> Result<Instruction, Error> {
        if self.pending.is_some() {
            return Err(Error::new(
                "LOOP_EFFECT_PENDING",
                "an effect has already been issued; it cannot be replayed",
            ));
        }
        if let Phase::Complete(value) = &self.phase {
            return Ok(Instruction::Complete {
                value: value.clone(),
            });
        }
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| Error::new("LOOP_ID_EXHAUSTED", "effect identity space exhausted"))?;
        let effect = |name: &str, arguments: Value| Effect {
            effect_id: self.sequence,
            name: name.into(),
            arguments,
        };
        let instruction = match &self.phase {
            Phase::Start(message) => {
                Instruction::Host(effect("session.start", json!({"message": message})))
            }
            Phase::Context => Instruction::Host(effect("context.prepare", json!({}))),
            Phase::Model(context) => Instruction::Model(effect("decide", context.clone())),
            Phase::Append { records, .. } => {
                Instruction::Host(effect("session.append", json!({"records": records})))
            }
            Phase::Tool(call) => Instruction::Tool(effect(&call.name, json!(call.arguments))),
            Phase::Failed => {
                return Err(Error::new("LOOP_TERMINAL", "failed loop cannot continue"));
            }
            Phase::Complete(_) => unreachable!(),
        };
        self.pending = Some(self.sequence);
        Ok(instruction)
    }

    fn accept(&mut self, effect_id: u64, result: Value) -> Result<(), Error> {
        if self.pending != Some(effect_id) {
            self.phase = Phase::Failed;
            self.pending = None;
            return Err(Error::new(
                "LOOP_CORRELATION",
                "result does not match the pending effect",
            ));
        }
        self.pending = None;
        let phase = std::mem::replace(&mut self.phase, Phase::Failed);
        self.phase = self.accept_phase(phase, result)?;
        Ok(())
    }

    fn snapshot(&self) -> Value {
        let phase = match self.phase {
            Phase::Start(_) => "start",
            Phase::Context => "context",
            Phase::Model(_) => "model",
            Phase::Append { .. } => "session",
            Phase::Tool(_) => "tool",
            Phase::Complete(_) => "complete",
            Phase::Failed => "failed",
        };
        json!({"phase": phase, "pending_effect": self.pending, "tool_count": self.tool_count})
    }
}

fn model_tools(config: LoopConfig) -> Result<Box<dyn LoopProgram>, Error> {
    Ok(Box::new(ServiceLoop::create(config, false)?))
}

fn single_tool(config: LoopConfig) -> Result<Box<dyn LoopProgram>, Error> {
    Ok(Box::new(ServiceLoop::create(config, true)?))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HandleRequest {
    loop_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AcceptRequest {
    loop_id: String,
    effect_id: u64,
    result: Value,
}

pub fn dispatch(command: &str, mut request: Value) -> Result<Value, Error> {
    static LOOPS: OnceLock<Mutex<LoopRegistry>> = OnceLock::new();
    let object = request
        .as_object_mut()
        .ok_or_else(|| Error::new("INVALID_REQUEST", "request must be an object"))?;
    object.remove("version");
    object.remove("command");
    let mut registry = LOOPS
        .get_or_init(|| Mutex::new(LoopRegistry::with_builtins()))
        .lock()
        .map_err(|_| Error::new("LOOP_UNAVAILABLE", "loop registry lock poisoned"))?;
    let decode_error = |error: serde_json::Error| Error::new("INVALID_REQUEST", error.to_string());
    match command {
        "loop_create" => registry.create(serde_json::from_value(request).map_err(decode_error)?),
        "loop_accept" => {
            let request: AcceptRequest = serde_json::from_value(request).map_err(decode_error)?;
            registry.accept(&request.loop_id, request.effect_id, request.result)
        }
        "loop_next" | "loop_snapshot" | "loop_close" => {
            let request: HandleRequest = serde_json::from_value(request).map_err(decode_error)?;
            match command {
                "loop_next" => serde_json::to_value(registry.next(&request.loop_id)?)
                    .map_err(|error| Error::new("INVALID_INSTRUCTION", error.to_string())),
                "loop_snapshot" => registry.snapshot(&request.loop_id),
                _ => Ok(registry.close(&request.loop_id)),
            }
        }
        _ => Err(Error::new(
            "UNKNOWN_COMMAND",
            format!("unknown loop command {command}"),
        )),
    }
}

#[cfg(test)]
#[path = "agent_loop_tests.rs"]
mod tests;
