use crate::{StorageError, fields, object};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub const MAX_DEPTH: usize = 32;
pub const MAX_ITEMS: usize = 10000;
pub const MAX_NODES: usize = 250000;
pub const MAX_BYTES: usize = 96 * 1024 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", content = "value", rename_all = "snake_case")]
pub enum StorageValue {
    Json(Value),
    Bytes(String),
    Mapping(BTreeMap<String, StorageValue>),
    Sequence(Vec<StorageValue>),
    Document(Document),
    ObjectReference(String),
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Document {
    pub id: String,
    pub revision: u64,
    pub value: Box<StorageValue>,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Null,
    Boolean,
    Integer,
    Number,
    String,
    Bytes,
    Mapping,
    Sequence,
    Document,
    ObjectReference,
}

#[derive(Clone, Debug)]
pub struct ValueInfo {
    pub kind: Kind,
    pub encoded_bytes: usize,
    pub length: usize,
    pub number: Option<String>,
    pub children: Vec<ValueInfo>,
}

impl ValueInfo {
    fn new(kind: Kind, encoded_bytes: usize) -> Self {
        Self {
            kind,
            encoded_bytes,
            length: 0,
            number: None,
            children: Vec::new(),
        }
    }
    pub fn integer(&self) -> Option<i128> {
        (self.kind == Kind::Integer)
            .then_some(self.number.as_deref())
            .flatten()?
            .parse()
            .ok()
    }
    pub fn number(&self) -> Option<f64> {
        self.number.as_deref()?.parse().ok()
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodecLimits {
    pub max_nodes: usize,
    pub max_bytes: usize,
}

impl Default for CodecLimits {
    fn default() -> Self {
        Self {
            max_nodes: MAX_NODES,
            max_bytes: MAX_BYTES,
        }
    }
}

impl CodecLimits {
    pub fn validate(self) -> Result<Self, StorageError> {
        if self.max_nodes == 0
            || self.max_nodes > MAX_NODES
            || self.max_bytes == 0
            || self.max_bytes > MAX_BYTES
        {
            return Err(limit());
        }
        Ok(self)
    }
}

#[derive(Debug)]
enum Frame {
    Container {
        kind: Kind,
        expected: usize,
        count: usize,
        bytes: usize,
        keys: BTreeSet<String>,
        pending_key: bool,
        children: Vec<ValueInfo>,
    },
    Document {
        count: usize,
        bytes: usize,
    },
    Text {
        kind: Kind,
        bytes: usize,
        length: usize,
        padded: bool,
        key: Option<String>,
    },
}

pub struct ValueCodec {
    limits: CodecLimits,
    nodes: usize,
    bytes: usize,
    stack: Vec<Frame>,
    roots: Vec<ValueInfo>,
}

fn invalid() -> StorageError {
    StorageError::new("STORAGE_INVALID_PROCESS_VALUE")
}
fn limit() -> StorageError {
    StorageError::new("STORAGE_PROCESS_LIMIT")
}
fn json_bytes(value: &Value) -> usize {
    serde_json::to_vec(value).expect("JSON value").len()
}
fn envelope(kind: &str) -> usize {
    20 + kind.len()
}
fn integer(value: &str) -> bool {
    let body = value.strip_prefix('-').unwrap_or(value);
    !body.is_empty()
        && body.bytes().all(|byte| byte.is_ascii_digit())
        && (body == "0" || !body.starts_with('0'))
}
fn base64(value: &str) -> Option<usize> {
    if !value.len().is_multiple_of(4) || !value.is_ascii() {
        return None;
    }
    let padding = value.bytes().rev().take_while(|byte| *byte == b'=').count();
    (padding <= 2
        && value.as_bytes()[..value.len() - padding]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/')))
    .then_some(value.len() / 4 * 3 - padding)
}
fn blob_reference(value: &str) -> bool {
    let parts = value.split(':').collect::<Vec<_>>();
    parts.len() == 6
        && parts[0] == "blob"
        && parts[1] == "v1"
        && parts[2..5].iter().all(|part| {
            part.len() == 64
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        && (parts[5] == "0"
            || (!parts[5].starts_with('0')
                && !parts[5].is_empty()
                && parts[5].bytes().all(|byte| byte.is_ascii_digit())))
}

impl ValueCodec {
    pub fn new(limits: CodecLimits) -> Result<Self, StorageError> {
        Ok(Self {
            limits: limits.validate()?,
            nodes: 0,
            bytes: 0,
            stack: Vec::new(),
            roots: Vec::new(),
        })
    }
    fn start_value(&mut self) -> Result<(), StorageError> {
        if self.stack.len() > MAX_DEPTH {
            return Err(limit());
        }
        let document_id = matches!(self.stack.last(), Some(Frame::Document { count: 0, .. }));
        if !document_id {
            self.nodes += 1;
        }
        if self.nodes > self.limits.max_nodes {
            return Err(limit());
        }
        match self.stack.last() {
            Some(Frame::Text { .. }) => Err(invalid()),
            Some(Frame::Container {
                kind: Kind::Mapping,
                pending_key: false,
                ..
            }) => Err(invalid()),
            Some(Frame::Container {
                count, expected, ..
            }) if count >= expected => Err(invalid()),
            Some(Frame::Document { count, .. }) if *count >= 2 => Err(invalid()),
            _ => Ok(()),
        }
    }
    fn accept(&mut self, info: ValueInfo) -> Result<(), StorageError> {
        match self.stack.last_mut() {
            Some(Frame::Container {
                kind,
                expected,
                count,
                bytes,
                pending_key,
                children,
                ..
            }) => {
                if *count >= *expected || (*kind == Kind::Mapping && !*pending_key) {
                    return Err(invalid());
                }
                *bytes += info.encoded_bytes + usize::from(*count > 0);
                *count += 1;
                *pending_key = false;
                if *kind == Kind::Sequence {
                    children.push(info);
                }
            }
            Some(Frame::Document { count, bytes }) => {
                let expected = if *count == 0 {
                    Kind::String
                } else {
                    Kind::Mapping
                };
                if *count >= 2 || info.kind != expected {
                    return Err(invalid());
                }
                *bytes += if *count == 0 {
                    info.encoded_bytes - envelope("json")
                } else {
                    info.encoded_bytes
                };
                *count += 1;
            }
            Some(Frame::Text { .. }) => return Err(invalid()),
            None => {
                self.bytes += info.encoded_bytes;
                if self.bytes > self.limits.max_bytes {
                    return Err(limit());
                }
                self.roots.push(info);
            }
        }
        if self.stack.iter().any(|frame| match frame {
            Frame::Container { bytes, .. }
            | Frame::Document { bytes, .. }
            | Frame::Text { bytes, .. } => *bytes > self.limits.max_bytes,
        }) {
            return Err(limit());
        }
        Ok(())
    }
    fn key(&mut self, key: String) -> Result<(), StorageError> {
        match self.stack.last_mut() {
            Some(Frame::Container {
                kind: Kind::Mapping,
                expected,
                count,
                keys,
                pending_key,
                bytes,
                ..
            }) if !*pending_key && *count < *expected => {
                *bytes += json_bytes(&json!(key)) + 1;
                if !keys.insert(key) {
                    return Err(invalid());
                }
                *pending_key = true;
                Ok(())
            }
            _ => Err(invalid()),
        }
    }
    pub fn push(&mut self, event: &Value) -> Result<(), StorageError> {
        let selected = object(event, "STORAGE_INVALID_PROCESS_VALUE")?;
        let action = selected
            .get("event")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        match action {
            "scalar" => {
                fields(
                    selected,
                    &["event", "type", "value"],
                    &[],
                    "STORAGE_INVALID_PROCESS_VALUE",
                )?;
                self.start_value()?;
                let value = &selected["value"];
                let (kind, size) = match selected["type"].as_str() {
                    Some("json") if value.is_null() => (Kind::Null, 4),
                    Some("json") if value.is_boolean() => (Kind::Boolean, json_bytes(value)),
                    Some("json") if value.is_number() => (Kind::Number, json_bytes(value)),
                    Some("integer") if value.as_str().is_some_and(integer) => {
                        (Kind::Integer, value.as_str().unwrap().len())
                    }
                    Some("object_reference") if value.as_str().is_some_and(blob_reference) => {
                        (Kind::ObjectReference, json_bytes(value))
                    }
                    _ => return Err(invalid()),
                };
                let tag = if kind == Kind::ObjectReference {
                    "object_reference"
                } else {
                    "json"
                };
                let mut info = ValueInfo::new(kind, envelope(tag) + size);
                if kind == Kind::Integer {
                    info.number = value.as_str().map(str::to_owned);
                }
                if kind == Kind::Number {
                    info.number = Some(value.to_string());
                }
                self.accept(info)
            }
            "key" => {
                fields(
                    selected,
                    &["event", "value"],
                    &[],
                    "STORAGE_INVALID_PROCESS_VALUE",
                )?;
                self.key(selected["value"].as_str().ok_or_else(invalid)?.into())
            }
            "open" => {
                let kind = selected
                    .get("type")
                    .and_then(Value::as_str)
                    .ok_or_else(invalid)?;
                if kind != "key" {
                    self.start_value()?;
                }
                let frame = match kind {
                    "mapping" | "sequence" => {
                        fields(
                            selected,
                            &["event", "type", "length"],
                            &[],
                            "STORAGE_INVALID_PROCESS_VALUE",
                        )?;
                        let expected = selected["length"]
                            .as_u64()
                            .filter(|value| *value <= MAX_ITEMS as u64)
                            .ok_or_else(limit)? as usize;
                        Frame::Container {
                            kind: if kind == "mapping" {
                                Kind::Mapping
                            } else {
                                Kind::Sequence
                            },
                            expected,
                            count: 0,
                            bytes: envelope(kind) + 2,
                            keys: BTreeSet::new(),
                            pending_key: false,
                            children: Vec::new(),
                        }
                    }
                    "document" => {
                        fields(
                            selected,
                            &["event", "type", "revision"],
                            &[],
                            "STORAGE_INVALID_PROCESS_VALUE",
                        )?;
                        let revision = selected["revision"]
                            .as_str()
                            .filter(|value| {
                                integer(value) && !value.starts_with('-') && *value != "0"
                            })
                            .ok_or_else(invalid)?;
                        Frame::Document {
                            count: 0,
                            bytes: envelope("document") + 28 + revision.len(),
                        }
                    }
                    "string" | "bytes" | "key" => {
                        fields(
                            selected,
                            &["event", "type"],
                            &[],
                            "STORAGE_INVALID_PROCESS_VALUE",
                        )?;
                        Frame::Text {
                            kind: if kind == "bytes" {
                                Kind::Bytes
                            } else {
                                Kind::String
                            },
                            bytes: 2,
                            length: 0,
                            padded: false,
                            key: (kind == "key").then(String::new),
                        }
                    }
                    _ => return Err(invalid()),
                };
                self.stack.push(frame);
                Ok(())
            }
            "chunk" => {
                fields(
                    selected,
                    &["event", "value"],
                    &[],
                    "STORAGE_INVALID_PROCESS_VALUE",
                )?;
                let value = selected["value"].as_str().ok_or_else(invalid)?;
                match self.stack.last_mut() {
                    Some(Frame::Text {
                        kind,
                        bytes,
                        length,
                        padded,
                        key,
                    }) => {
                        if *kind == Kind::Bytes {
                            if *padded {
                                return Err(invalid());
                            }
                            *length += base64(value).ok_or_else(invalid)?;
                            *padded = value.ends_with('=');
                        } else {
                            *length += value.chars().count();
                        }
                        *bytes += json_bytes(&json!(value)) - 2;
                        if *bytes > self.limits.max_bytes {
                            return Err(limit());
                        }
                        if let Some(key) = key {
                            key.push_str(value);
                        }
                        Ok(())
                    }
                    _ => Err(invalid()),
                }
            }
            "close" => {
                fields(selected, &["event"], &[], "STORAGE_INVALID_PROCESS_VALUE")?;
                let info = match self.stack.pop().ok_or_else(invalid)? {
                    Frame::Container {
                        kind,
                        expected,
                        count,
                        bytes,
                        pending_key,
                        children,
                        ..
                    } => {
                        if count != expected || pending_key {
                            return Err(invalid());
                        }
                        ValueInfo {
                            kind,
                            encoded_bytes: bytes,
                            length: count,
                            number: None,
                            children,
                        }
                    }
                    Frame::Document { count, bytes } => {
                        if count != 2 {
                            return Err(invalid());
                        }
                        ValueInfo::new(Kind::Document, bytes)
                    }
                    Frame::Text { key: Some(key), .. } => return self.key(key),
                    Frame::Text {
                        kind,
                        bytes,
                        length,
                        ..
                    } => ValueInfo {
                        kind,
                        encoded_bytes: bytes
                            + envelope(if kind == Kind::Bytes { "bytes" } else { "json" }),
                        length,
                        number: None,
                        children: Vec::new(),
                    },
                };
                self.accept(info)
            }
            _ => Err(invalid()),
        }
    }
    pub fn finish(self) -> Result<Vec<ValueInfo>, StorageError> {
        if !self.stack.is_empty() {
            return Err(invalid());
        }
        Ok(self.roots)
    }
}

impl StorageValue {
    pub fn validate(&self) -> Result<(), StorageError> {
        self.inspect(CodecLimits::default()).map(|_| ())
    }
    pub fn inspect(&self, limits: CodecLimits) -> Result<ValueInfo, StorageError> {
        let mut codec = ValueCodec::new(limits)?;
        self.visit(&mut codec, 0)?;
        codec.finish()?.pop().ok_or_else(invalid)
    }
    pub(crate) fn visit(&self, codec: &mut ValueCodec, depth: usize) -> Result<(), StorageError> {
        if depth > MAX_DEPTH {
            return Err(limit());
        }
        match self {
            Self::Json(Value::String(value)) | Self::Bytes(value) => {
                codec.push(&json!({"event":"open","type":if matches!(self, Self::Bytes(_)) {"bytes"} else {"string"}}))?;
                let mut start = 0;
                while start < value.len() {
                    let mut end = (start + 32768).min(value.len());
                    while !value.is_char_boundary(end) {
                        end -= 1;
                    }
                    codec.push(&json!({"event":"chunk","value":&value[start..end]}))?;
                    start = end;
                }
                codec.push(&json!({"event":"close"}))
            }
            Self::Json(value) => {
                if value.is_i64() || value.is_u64() {
                    codec
                        .push(&json!({"event":"scalar","type":"integer","value":value.to_string()}))
                } else {
                    codec.push(&json!({"event":"scalar","type":"json","value":value}))
                }
            }
            Self::ObjectReference(value) => {
                codec.push(&json!({"event":"scalar","type":"object_reference","value":value}))
            }
            Self::Mapping(values) => {
                codec.push(&json!({"event":"open","type":"mapping","length":values.len()}))?;
                for (key, value) in values {
                    codec.push(&json!({"event":"key","value":key}))?;
                    value.visit(codec, depth + 1)?;
                }
                codec.push(&json!({"event":"close"}))
            }
            Self::Sequence(values) => {
                codec.push(&json!({"event":"open","type":"sequence","length":values.len()}))?;
                for value in values {
                    value.visit(codec, depth + 1)?;
                }
                codec.push(&json!({"event":"close"}))
            }
            Self::Document(value) => {
                codec.push(&json!({"event":"open","type":"document","revision":value.revision.to_string()}))?;
                StorageValue::Json(json!(value.id)).visit(codec, depth + 1)?;
                value.value.visit(codec, depth + 1)?;
                codec.push(&json!({"event":"close"}))
            }
        }
    }
}
