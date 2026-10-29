//! Bounded graph execution. Domain callbacks choose declared routes; Rust owns progression.

use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::Error;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub kind: String,
    pub routes: BTreeMap<String, Option<String>>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphConfig {
    pub start: String,
    pub nodes: BTreeMap<String, Node>,
}

fn valid_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 128
}

struct Graph {
    nodes: BTreeMap<String, Node>,
    current: Option<String>,
    pending: Option<u64>,
    sequence: u64,
}

impl Graph {
    fn create(config: GraphConfig) -> Result<Self, Error> {
        if config.nodes.is_empty()
            || config.nodes.len() > 256
            || !config.nodes.contains_key(&config.start)
        {
            return Err(Error::new(
                "INVALID_GRAPH",
                "graph requires an existing start and 1..256 nodes",
            ));
        }
        for (name, node) in &config.nodes {
            if !valid_name(name)
                || !matches!(node.kind.as_str(), "host" | "model" | "tool")
                || node.routes.is_empty()
                || node.routes.len() > 256
                || node.routes.iter().any(|(route, target)| {
                    !valid_name(route)
                        || target
                            .as_ref()
                            .is_some_and(|target| !config.nodes.contains_key(target))
                })
            {
                return Err(Error::new(
                    "INVALID_GRAPH",
                    "invalid node kind, name or route target",
                ));
            }
        }
        Ok(Self {
            nodes: config.nodes,
            current: Some(config.start),
            pending: None,
            sequence: 0,
        })
    }

    fn next(&mut self) -> Result<Value, Error> {
        if self.pending.is_some() {
            return Err(Error::new(
                "GRAPH_RESULT_PENDING",
                "settle the dispatched node before advancing",
            ));
        }
        let Some(name) = self.current.as_ref() else {
            return Ok(json!({"kind": "complete"}));
        };
        self.sequence = self.sequence.checked_add(1).ok_or_else(|| {
            Error::new(
                "GRAPH_SEQUENCE_EXHAUSTED",
                "effect identity space exhausted",
            )
        })?;
        self.pending = Some(self.sequence);
        Ok(json!({"kind": self.nodes[name].kind, "name": name, "effect_id": self.sequence}))
    }

    fn accept(&mut self, effect_id: u64, route: &str) -> Result<Value, Error> {
        if self.pending != Some(effect_id) {
            return Err(Error::new(
                "GRAPH_RESULT_MISMATCH",
                "result does not match the dispatched effect",
            ));
        }
        let current = self.current.as_ref().ok_or_else(|| {
            Error::new("GRAPH_COMPLETE", "completed graph cannot accept a result")
        })?;
        let target = self.nodes[current].routes.get(route).ok_or_else(|| {
            Error::new("GRAPH_ROUTE_NOT_FOUND", "node did not declare this route")
        })?;
        self.current = target.clone();
        self.pending = None;
        Ok(self.snapshot())
    }

    fn snapshot(&self) -> Value {
        json!({"node": self.current, "pending": self.pending, "operations_started": self.sequence})
    }
}

#[derive(Default)]
pub struct GraphRegistry {
    graphs: BTreeMap<String, Graph>,
    sequence: u64,
}

impl GraphRegistry {
    pub fn create(&mut self, config: GraphConfig) -> Result<Value, Error> {
        if self.graphs.len() >= 4096 {
            return Err(Error::new(
                "GRAPH_CAPACITY",
                "close unused graphs before creating more",
            ));
        }
        let graph = Graph::create(config)?;
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| Error::new("GRAPH_ID_EXHAUSTED", "graph identity space exhausted"))?;
        let id = format!("graph-{}", self.sequence);
        self.graphs.insert(id.clone(), graph);
        Ok(json!({"graph_id": id}))
    }

    fn graph_mut(&mut self, id: &str) -> Result<&mut Graph, Error> {
        self.graphs
            .get_mut(id)
            .ok_or_else(|| Error::new("GRAPH_NOT_FOUND", "graph has closed or does not exist"))
    }

    pub fn next(&mut self, id: &str) -> Result<Value, Error> {
        let result = self.graph_mut(id)?.next();
        if result.is_err() {
            self.graphs.remove(id);
        }
        result
    }

    pub fn accept(&mut self, id: &str, effect_id: u64, route: &str) -> Result<Value, Error> {
        let result = self.graph_mut(id)?.accept(effect_id, route);
        if result.is_err() {
            self.graphs.remove(id);
        }
        result
    }

    pub fn snapshot(&mut self, id: &str) -> Result<Value, Error> {
        Ok(self.graph_mut(id)?.snapshot())
    }

    pub fn close(&mut self, id: &str) -> Value {
        json!({"closed": self.graphs.remove(id).is_some()})
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HandleRequest {
    graph_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AcceptRequest {
    graph_id: String,
    effect_id: u64,
    route: String,
}

pub fn dispatch(command: &str, mut request: Value) -> Result<Value, Error> {
    static GRAPHS: OnceLock<Mutex<GraphRegistry>> = OnceLock::new();
    let object = request
        .as_object_mut()
        .ok_or_else(|| Error::new("INVALID_REQUEST", "request must be an object"))?;
    object.remove("version");
    object.remove("command");
    let mut registry = GRAPHS
        .get_or_init(|| Mutex::new(GraphRegistry::default()))
        .lock()
        .map_err(|_| Error::new("GRAPH_UNAVAILABLE", "graph registry lock poisoned"))?;
    let decode_error = |error: serde_json::Error| Error::new("INVALID_REQUEST", error.to_string());
    match command {
        "graph_create" => registry.create(serde_json::from_value(request).map_err(decode_error)?),
        "graph_accept" => {
            let request: AcceptRequest = serde_json::from_value(request).map_err(decode_error)?;
            registry.accept(&request.graph_id, request.effect_id, &request.route)
        }
        "graph_next" | "graph_snapshot" | "graph_close" => {
            let request: HandleRequest = serde_json::from_value(request).map_err(decode_error)?;
            match command {
                "graph_next" => registry.next(&request.graph_id),
                "graph_snapshot" => registry.snapshot(&request.graph_id),
                _ => Ok(registry.close(&request.graph_id)),
            }
        }
        _ => Err(Error::new(
            "UNKNOWN_COMMAND",
            format!("unknown graph command {command}"),
        )),
    }
}

#[cfg(test)]
#[path = "graph_tests.rs"]
mod tests;
