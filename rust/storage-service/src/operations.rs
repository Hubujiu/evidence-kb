use crate::{
    StorageError,
    contracts::Contract,
    values::{Kind, ValueInfo},
};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Effect {
    Read,
    Write,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Channel {
    Value,
    Structured,
    Object,
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Rule {
    Any,
    Null,
    Boolean,
    Text,
    Bytes,
    Mapping,
    Document,
    ObjectReference,
    Revision,
    PositiveInteger,
    PositiveNumber,
    QueryLimit,
    Strings,
    Records,
    Documents,
    Vector,
}

impl Rule {
    pub fn accepts(self, value: &ValueInfo) -> bool {
        match self {
            Self::Any => true,
            Self::Null => value.kind == Kind::Null,
            Self::Boolean => value.kind == Kind::Boolean,
            Self::Text => value.kind == Kind::String,
            Self::Bytes => value.kind == Kind::Bytes,
            Self::Mapping => value.kind == Kind::Mapping,
            Self::Document => value.kind == Kind::Document,
            Self::ObjectReference => value.kind == Kind::ObjectReference,
            Self::Revision => value.integer().is_some_and(|number| number >= 0),
            Self::PositiveInteger => value.integer().is_some_and(|number| number > 0),
            Self::PositiveNumber => value
                .number()
                .is_some_and(|number| number.is_finite() && number > 0.0),
            Self::QueryLimit => value
                .integer()
                .is_some_and(|number| (1..=1000).contains(&number)),
            Self::Strings => {
                value.kind == Kind::Sequence
                    && value.children.iter().all(|item| item.kind == Kind::String)
            }
            Self::Records => {
                value.kind == Kind::Sequence
                    && value.children.iter().all(|item| item.kind == Kind::Mapping)
            }
            Self::Documents => {
                value.kind == Kind::Sequence
                    && value
                        .children
                        .iter()
                        .all(|item| matches!(item.kind, Kind::Document | Kind::Null))
            }
            Self::Vector => {
                value.kind == Kind::Sequence
                    && value
                        .children
                        .iter()
                        .all(|item| matches!(item.kind, Kind::Integer | Kind::Number))
            }
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Parameter {
    pub name: &'static str,
    pub rule: Rule,
    pub required: bool,
    pub keyword_only: bool,
    pub nullable: bool,
}

fn parameter(name: &'static str, rule: Rule) -> Parameter {
    Parameter {
        name,
        rule,
        required: true,
        keyword_only: false,
        nullable: false,
    }
}
fn optional(name: &'static str, rule: Rule, keyword_only: bool) -> Parameter {
    Parameter {
        name,
        rule,
        required: false,
        keyword_only,
        nullable: true,
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Operation {
    pub method: &'static str,
    pub channel: Channel,
    pub effect: Effect,
    pub parameters: Vec<Parameter>,
    pub result: Rule,
    pub nullable_result: bool,
    #[serde(skip)]
    pub document_receipt: bool,
}

fn operation(
    method: &'static str,
    effect: Effect,
    parameters: Vec<Parameter>,
    result: Rule,
) -> Operation {
    Operation {
        method,
        channel: Channel::Value,
        effect,
        parameters,
        result,
        nullable_result: false,
        document_receipt: false,
    }
}

pub fn catalogue(contract: &Contract) -> BTreeMap<String, Operation> {
    let mut selected = BTreeMap::new();
    for parent in &contract.extends {
        selected.extend(catalogue(parent));
    }
    if contract.major != 1 {
        return selected;
    }
    let base = match contract.id.as_str() {
        "storage.kv.expiring" | "storage.kv.durable" => Some("storage.kv"),
        "storage.relational.guarded" => Some("storage.relational.structured"),
        "storage.relational.postgres" => Some("storage.relational"),
        "storage.lexical.bm25" => Some("storage.lexical"),
        _ => None,
    };
    if let Some(id) = base {
        selected.extend(catalogue(&Contract {
            id: id.into(),
            major: 1,
            extends: vec![],
        }));
    }
    use Effect::{Read, Write};
    use Rule::*;
    let methods = match contract.id.as_str() {
        "storage.kv" => vec![
            operation("get", Read, vec![parameter("key", Text)], Bytes),
            operation(
                "set",
                Write,
                vec![parameter("key", Text), parameter("value", Bytes)],
                Null,
            ),
            operation("delete", Write, vec![parameter("key", Text)], Boolean),
        ],
        "storage.kv.expiring" => vec![operation(
            "put_expiring",
            Write,
            vec![
                parameter("key", Text),
                parameter("value", Bytes),
                Parameter {
                    keyword_only: true,
                    ..parameter("ttl_seconds", PositiveNumber)
                },
            ],
            Null,
        )],
        "storage.document" => vec![
            operation("dependency_manifest", Read, vec![], Mapping),
            operation("get", Read, vec![parameter("document_id", Text)], Document),
            operation(
                "get_many",
                Read,
                vec![parameter("document_ids", Strings)],
                Documents,
            ),
            Operation {
                document_receipt: true,
                ..operation(
                    "put",
                    Write,
                    vec![
                        parameter("document_id", Text),
                        parameter("value", Mapping),
                        optional("expected_revision", Revision, true),
                    ],
                    Document,
                )
            },
            operation(
                "delete",
                Write,
                vec![
                    parameter("document_id", Text),
                    optional("expected_revision", Revision, true),
                ],
                Boolean,
            ),
        ],
        "storage.vector" | "storage.lexical" => vec![
            operation("dependency_manifest", Read, vec![], Mapping),
            operation(
                "stage",
                Write,
                vec![
                    parameter("index", Text),
                    parameter("records", Records),
                    optional("vector_dimension", PositiveInteger, false),
                ],
                Null,
            ),
            operation("verify", Read, vec![parameter("index", Text)], Mapping),
            operation(
                "seal",
                Write,
                vec![parameter("index", Text), parameter("checksum", Text)],
                Null,
            ),
            operation(
                "is_sealed",
                Read,
                vec![parameter("index", Text), parameter("checksum", Text)],
                Boolean,
            ),
            operation(
                "read_records",
                Read,
                vec![parameter("index", Text), optional("ids", Strings, false)],
                Records,
            ),
            operation(
                "query",
                Read,
                vec![
                    parameter("index", Text),
                    parameter(
                        "query",
                        if contract.id == "storage.vector" {
                            Vector
                        } else {
                            Text
                        },
                    ),
                    parameter("limit", QueryLimit),
                    optional("filters", Mapping, true),
                ],
                Records,
            ),
        ],
        "storage.object" => vec![
            Operation {
                channel: Channel::Object,
                ..operation(
                    "put",
                    Write,
                    vec![parameter("tenant_id", Text), parameter("data", Bytes)],
                    ObjectReference,
                )
            },
            operation(
                "reference",
                Read,
                vec![
                    parameter("tenant_id", Text),
                    parameter("locator", Text),
                    optional("sha256", Text, true),
                ],
                ObjectReference,
            ),
            Operation {
                channel: Channel::Object,
                ..operation(
                    "read",
                    Read,
                    vec![
                        parameter("tenant_id", Text),
                        parameter("locator", Text),
                        optional("sha256", Text, true),
                    ],
                    Bytes,
                )
            },
            operation(
                "stat",
                Read,
                vec![
                    parameter("tenant_id", Text),
                    parameter("locator", Text),
                    optional("sha256", Text, true),
                ],
                ObjectReference,
            ),
        ],
        "storage.relational" => vec![
            operation(
                "query_json",
                Read,
                vec![parameter("request", Mapping)],
                Mapping,
            ),
            operation(
                "batch_json",
                Write,
                vec![parameter("request", Mapping)],
                Mapping,
            ),
            Operation {
                channel: Channel::Structured,
                ..operation("prepare", Write, vec![], Any)
            },
            Operation {
                channel: Channel::Structured,
                ..operation("seal", Write, vec![], Any)
            },
            Operation {
                channel: Channel::Structured,
                ..operation("transaction", Write, vec![], Any)
            },
            Operation {
                channel: Channel::Structured,
                ..operation("batch", Write, vec![], Any)
            },
        ],
        "storage.relational.structured" => vec![
            Operation {
                channel: Channel::Structured,
                ..operation("prepare", Write, vec![], Any)
            },
            Operation {
                channel: Channel::Structured,
                ..operation("seal", Write, vec![], Any)
            },
            Operation {
                channel: Channel::Structured,
                ..operation("transaction", Write, vec![], Any)
            },
        ],
        "storage.relational.guarded" => vec![Operation {
            channel: Channel::Structured,
            ..operation("guard", Read, vec![], Any)
        }],
        _ => vec![],
    };
    for mut item in methods {
        if item.method == "get" {
            item.nullable_result = true;
        }
        selected.insert(item.method.into(), item);
    }
    selected
}

impl Operation {
    pub fn bind(&self, argc: usize, keywords: &[String]) -> Result<Vec<usize>, StorageError> {
        if self.channel == Channel::Structured {
            return Err(StorageError::new("STORAGE_PROCESS_METHOD_REJECTED"));
        }
        if argc > 64 || keywords.len() > 64 {
            return Err(StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"));
        }
        let mut bound = Vec::new();
        let mut used = BTreeSet::new();
        for index in 0..argc {
            if self
                .parameters
                .get(index)
                .is_none_or(|parameter| parameter.keyword_only)
            {
                return Err(StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"));
            }
            bound.push(index);
            used.insert(index);
        }
        for name in keywords {
            let index = self
                .parameters
                .iter()
                .position(|parameter| parameter.name == name)
                .ok_or_else(|| StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"))?;
            if !used.insert(index) {
                return Err(StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"));
            }
            bound.push(index);
        }
        if self
            .parameters
            .iter()
            .enumerate()
            .any(|(index, parameter)| parameter.required && !used.contains(&index))
        {
            return Err(StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"));
        }
        Ok(bound)
    }
    pub fn validate_input(
        &self,
        bound: &[usize],
        values: &[ValueInfo],
        result_limit: usize,
    ) -> Result<(), StorageError> {
        if values.len() != bound.len() {
            return Err(StorageError::new("STORAGE_INVALID_PROCESS_REQUEST"));
        }
        for (index, value) in bound.iter().zip(values) {
            let parameter = &self.parameters[*index];
            if !(parameter.rule.accepts(value) || parameter.nullable && value.kind == Kind::Null) {
                let code = if matches!(parameter.rule, Rule::QueryLimit) {
                    "STORAGE_PROCESS_LIMIT"
                } else {
                    "STORAGE_INVALID_PROCESS_REQUEST"
                };
                return Err(StorageError::new(code));
            }
        }
        if self.document_receipt {
            let id = values
                .get(
                    bound
                        .iter()
                        .position(|index| *index == 0)
                        .expect("required argument"),
                )
                .unwrap();
            let value = values
                .get(
                    bound
                        .iter()
                        .position(|index| *index == 1)
                        .expect("required argument"),
                )
                .unwrap();
            if id.encoded_bytes + value.encoded_bytes + 52 > result_limit {
                return Err(StorageError::new("STORAGE_PROCESS_LIMIT"));
            }
        }
        Ok(())
    }
    pub fn validate_result(&self, value: &ValueInfo) -> Result<(), StorageError> {
        if self.result.accepts(value) || self.nullable_result && value.kind == Kind::Null {
            Ok(())
        } else {
            Err(StorageError::new("STORAGE_IMPLEMENTATION_RESULT_INVALID"))
        }
    }
    pub fn result_failure(&self, error: StorageError) -> StorageError {
        if self.effect == Effect::Read {
            return error;
        }
        let mut completed = StorageError::new("STORAGE_WRITE_RESULT_UNAVAILABLE");
        completed
            .details
            .insert("outcome".into(), serde_json::json!("COMPLETED"));
        completed
            .details
            .insert("method".into(), serde_json::json!(self.method));
        completed
    }
}
