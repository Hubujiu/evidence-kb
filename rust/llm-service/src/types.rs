use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;

pub const PROTOCOL: &str = "llm-service/1";
pub const MAX_BLOCKS: usize = 1024;
pub const MAX_CONTENT_BYTES: usize = 2 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LlmError {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_retry_after_ms: Option<u64>,
}

impl LlmError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            status: None,
            request_id: None,
            provider_retry_after_ms: None,
        }
    }
}

impl std::fmt::Display for LlmError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for LlmError {}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum BlockType {
    Text,
    Reasoning,
    Image,
    File,
    ToolCall,
    ToolAddition,
    ToolRemoval,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case", deny_unknown_fields)]
pub enum ContentBlock {
    Text {
        text: String,
    },
    Reasoning {
        text: String,
    },
    Image {
        attachment: Value,
    },
    File {
        attachment: Value,
    },
    ToolCall {
        id: String,
        name: String,
        arguments: String,
    },
    ToolAddition {
        #[serde(rename = "toolName")]
        tool_name: String,
    },
    ToolRemoval {
        #[serde(rename = "toolName")]
        tool_name: String,
    },
}

impl ContentBlock {
    pub fn kind(&self) -> BlockType {
        match self {
            Self::Text { .. } => BlockType::Text,
            Self::Reasoning { .. } => BlockType::Reasoning,
            Self::Image { .. } => BlockType::Image,
            Self::File { .. } => BlockType::File,
            Self::ToolCall { .. } => BlockType::ToolCall,
            Self::ToolAddition { .. } => BlockType::ToolAddition,
            Self::ToolRemoval { .. } => BlockType::ToolRemoval,
        }
    }

    pub fn is_safe_text(&self) -> bool {
        matches!(self, Self::Text { text } | Self::Reasoning { text } if !text.trim().is_empty())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    System,
    Developer,
    User,
    Assistant,
    Tool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReplayEnvelope {
    pub response: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub blocks: Option<Vec<Value>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ModelSource {
    pub provider: String,
    pub model: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub adapter_identity: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replay_state: Option<ReplayEnvelope>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Message {
    pub role: Role,
    pub content: Vec<ContentBlock>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<ModelSource>,
}

impl Message {
    pub fn text(role: Role, text: impl Into<String>) -> Self {
        Self {
            role,
            content: vec![ContentBlock::Text { text: text.into() }],
            tool_call_id: None,
            source: None,
            id: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolSchema {
    pub name: String,
    pub description: String,
    pub parameters: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub defer_loading: Option<bool>,
}

fn valid_tool_schemas(tools: &[ToolSchema]) -> bool {
    let mut names = BTreeSet::new();
    tools.iter().all(|tool| {
        !tool.name.is_empty()
            && tool.parameters.is_object()
            && names.insert(&tool.name)
            && tool.defer_loading != Some(false)
    })
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolHistoryUpdate {
    pub message_id: String,
    pub additions: Vec<ToolSchema>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ToolHistory {
    pub tools: Vec<ToolSchema>,
    pub updates: Vec<ToolHistoryUpdate>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ToolUpdate {
    AdditionOnly,
    InHistory,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SystemPromptUpdate {
    InHistory,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Request {
    pub provider: String,
    pub model: String,
    pub messages: Vec<Message>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tools: Vec<ToolSchema>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_history: Option<ToolHistory>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning_effort: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stop: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub purpose: Option<String>,
}

impl Request {
    pub fn text(
        provider: impl Into<String>,
        model: impl Into<String>,
        text: impl Into<String>,
    ) -> Self {
        Self {
            provider: provider.into(),
            model: model.into(),
            messages: vec![Message::text(Role::User, text)],
            system: None,
            tools: vec![],
            tool_history: None,
            temperature: None,
            max_tokens: None,
            reasoning_effort: None,
            stop: vec![],
            session_id: None,
            purpose: None,
        }
    }

    pub fn validate(&self, capabilities: &Capabilities) -> Result<(), LlmError> {
        if self.provider.is_empty()
            || self.model.is_empty()
            || self.messages.is_empty()
            || self.messages.len() > MAX_BLOCKS
            || self.max_tokens == Some(0)
            || self.temperature.is_some_and(|v| !v.is_finite() || v < 0.0)
        {
            return Err(LlmError::new(
                "INVALID_LLM_REQUEST",
                "invalid model request",
            ));
        }
        for (present, name) in [
            (self.system.is_some(), "system"),
            (self.max_tokens.is_some(), "maxTokens"),
            (self.temperature.is_some(), "temperature"),
            (!self.stop.is_empty(), "stop"),
            (self.reasoning_effort.is_some(), "reasoningEffort"),
        ] {
            if present && !capabilities.options.contains(name) {
                return Err(LlmError::new("UNSUPPORTED_LLM_OPTION", name));
            }
        }
        if (!self.tools.is_empty() || self.tool_history.is_some()) && !capabilities.tools {
            return Err(LlmError::new(
                "UNSUPPORTED_LLM_TOOLS",
                "route does not support tools",
            ));
        }
        if !valid_tool_schemas(&self.tools) {
            return Err(LlmError::new(
                "INVALID_LLM_TOOL",
                "invalid or duplicate tool schema",
            ));
        }
        if self.tool_history.is_some() && capabilities.tool_update.is_none() {
            return Err(LlmError::new("UNSUPPORTED_LLM_OPTION", "toolHistory"));
        }
        if let Some(history) = &self.tool_history {
            if !valid_tool_schemas(&history.tools) {
                return Err(LlmError::new(
                    "INVALID_LLM_TOOL_HISTORY",
                    "invalid or duplicate historical baseline",
                ));
            }
            let mut updates = BTreeSet::new();
            for update in &history.updates {
                if update.message_id.is_empty()
                    || !updates.insert(&update.message_id)
                    || !valid_tool_schemas(&update.additions)
                {
                    return Err(LlmError::new(
                        "INVALID_LLM_TOOL_HISTORY",
                        "invalid historical tool declaration",
                    ));
                }
            }
        }
        let mut blocks = 0;
        for (position, message) in self.messages.iter().enumerate() {
            if message.content.is_empty()
                || (message.role == Role::Tool
                    && message.tool_call_id.as_ref().is_none_or(String::is_empty))
            {
                return Err(LlmError::new(
                    "INVALID_LLM_MESSAGE",
                    "missing message content or tool identity",
                ));
            }
            if position > 0
                && message.role == Role::System
                && capabilities.system_prompt_update.is_none()
            {
                return Err(LlmError::new(
                    "UNSUPPORTED_LLM_OPTION",
                    "systemPromptUpdate",
                ));
            }
            blocks += message.content.len();
            for block in &message.content {
                let supported = match block {
                    ContentBlock::Text { .. } => true,
                    ContentBlock::Reasoning { .. } => capabilities.reasoning,
                    ContentBlock::Image { .. } => capabilities.images,
                    ContentBlock::File { .. } => false,
                    ContentBlock::ToolCall {
                        id,
                        name,
                        arguments,
                    } => {
                        if message.role != Role::Assistant
                            || id.is_empty()
                            || name.is_empty()
                            || !serde_json::from_str::<Value>(arguments)
                                .is_ok_and(|v| v.is_object())
                        {
                            return Err(LlmError::new(
                                "INVALID_LLM_TOOL_CALL",
                                "tool calls require complete JSON objects",
                            ));
                        }
                        capabilities.tools
                    }
                    ContentBlock::ToolAddition { tool_name } => {
                        if message.role != Role::Developer
                            || tool_name.is_empty()
                            || self.tool_history.as_ref().is_none_or(|history| {
                                !history.updates.iter().any(|update| {
                                    Some(&update.message_id) == message.id.as_ref()
                                        && update
                                            .additions
                                            .iter()
                                            .any(|tool| &tool.name == tool_name)
                                })
                            })
                        {
                            return Err(LlmError::new(
                                "INVALID_LLM_TOOL_HISTORY",
                                "addition must reference its historical declaration",
                            ));
                        }
                        capabilities.tools && capabilities.tool_update.is_some()
                    }
                    ContentBlock::ToolRemoval { tool_name } => {
                        if message.role != Role::Developer
                            || tool_name.is_empty()
                            || self.tool_history.is_none()
                        {
                            return Err(LlmError::new(
                                "INVALID_LLM_TOOL_HISTORY",
                                "invalid historical removal",
                            ));
                        }
                        capabilities.tools
                            && capabilities.tool_update == Some(ToolUpdate::InHistory)
                    }
                };
                if !supported {
                    return Err(LlmError::new(
                        "UNSUPPORTED_LLM_CONTENT",
                        format!("{:?}", block.kind()),
                    ));
                }
            }
        }
        if blocks > MAX_BLOCKS
            || serde_json::to_vec(self).map_or(true, |v| v.len() > MAX_CONTENT_BYTES)
        {
            return Err(LlmError::new(
                "LLM_REQUEST_TOO_LARGE",
                "request exceeds protocol limits",
            ));
        }
        Ok(())
    }

    pub fn for_adapter(mut self, adapter_identity: &str) -> Self {
        for message in &mut self.messages {
            if let Some(source) = &mut message.source
                && (adapter_identity.is_empty()
                    || source.provider != self.provider
                    || source.model != self.model
                    || source.adapter_identity.as_deref() != Some(adapter_identity))
            {
                source.replay_state = None;
            }
        }
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Capabilities {
    pub streaming: bool,
    #[serde(default)]
    pub tools: bool,
    #[serde(default)]
    pub reasoning: bool,
    #[serde(default)]
    pub images: bool,
    #[serde(default)]
    pub options: BTreeSet<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_update: Option<ToolUpdate>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system_prompt_update: Option<SystemPromptUpdate>,
}

impl Default for Capabilities {
    fn default() -> Self {
        Self {
            streaming: true,
            tools: false,
            reasoning: false,
            images: false,
            options: ["maxTokens".to_owned(), "system".to_owned()].into(),
            tool_update: None,
            system_prompt_update: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ModelInfo {
    pub provider: String,
    pub id: String,
    pub name: String,
    pub capabilities: Capabilities,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context: Option<ModelContext>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub default_max_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning: Option<ModelReasoning>,
}

impl ModelInfo {
    pub fn basic(provider: &str, model: &str) -> Self {
        Self {
            provider: provider.into(),
            id: model.into(),
            name: model.into(),
            capabilities: Default::default(),
            context: None,
            default_max_tokens: None,
            reasoning: None,
        }
    }

    pub fn apply_defaults(&self, request: &mut Request) -> Result<(), LlmError> {
        if self
            .context
            .as_ref()
            .is_some_and(|context| context.context_window == 0)
            || self.default_max_tokens == Some(0)
        {
            return Err(LlmError::new(
                "INVALID_LLM_MODEL_METADATA",
                "model limits must be positive",
            ));
        }
        if request.max_tokens.is_none() {
            request.max_tokens = self.default_max_tokens;
        }
        if let Some(reasoning) = &self.reasoning {
            let mut ids = BTreeSet::new();
            if reasoning.efforts.iter().any(|effort| {
                effort.id.is_empty() || effort.name.is_empty() || !ids.insert(&effort.id)
            }) || reasoning
                .default_effort
                .as_ref()
                .is_some_and(|default| !ids.contains(default))
            {
                return Err(LlmError::new(
                    "INVALID_LLM_MODEL_METADATA",
                    "invalid reasoning effort catalog",
                ));
            }
            if request.reasoning_effort.is_none() {
                request.reasoning_effort = reasoning.default_effort.clone();
            }
            if request
                .reasoning_effort
                .as_ref()
                .is_some_and(|effort| !ids.contains(effort))
            {
                return Err(LlmError::new(
                    "UNSUPPORTED_LLM_REASONING_EFFORT",
                    "effort is not supported by this model",
                ));
            }
        } else if request.reasoning_effort.is_some() {
            return Err(LlmError::new(
                "UNSUPPORTED_LLM_REASONING_EFFORT",
                "model has no reasoning effort catalog",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ModelContext {
    pub context_window: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReasoningEffort {
    pub id: String,
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ModelReasoning {
    pub efforts: Vec<ReasoningEffort>,
    pub default_effort: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TokenUsage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_read_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_write_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning_tokens: Option<u64>,
}

impl TokenUsage {
    pub fn validate(&self) -> Result<(), LlmError> {
        if let Some(total) = self.total_tokens {
            let minimum = self
                .input_tokens
                .checked_add(self.output_tokens)
                .and_then(|v| v.checked_add(self.cache_read_tokens.unwrap_or(0)))
                .and_then(|v| v.checked_add(self.cache_write_tokens.unwrap_or(0)));
            if minimum.is_none_or(|v| total < v) {
                return Err(LlmError::new(
                    "INVALID_LLM_USAGE",
                    "total tokens contradict reported counters",
                ));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum FinishReason {
    Stop,
    ToolCalls,
    MaxTokens,
    Aborted { failure: LlmError },
    Error { failure: LlmError },
}

impl FinishReason {
    pub fn successful(&self) -> bool {
        matches!(self, Self::Stop | Self::ToolCalls | Self::MaxTokens)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case", deny_unknown_fields)]
pub enum StreamEvent {
    BlockStart {
        index: u32,
        #[serde(rename = "blockType")]
        block_type: BlockType,
    },
    TextDelta {
        index: u32,
        text: String,
    },
    ReasoningDelta {
        index: u32,
        text: String,
    },
    ToolCallDelta {
        index: u32,
        id: String,
        name: Option<String>,
        #[serde(rename = "argumentsDelta")]
        arguments_delta: String,
    },
    BlockEnd {
        index: u32,
        block: ContentBlock,
    },
    Usage {
        usage: TokenUsage,
    },
    Finish {
        reason: FinishReason,
        #[serde(
            default,
            rename = "replayState",
            skip_serializing_if = "Option::is_none"
        )]
        replay_state: Option<ReplayEnvelope>,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Response {
    pub provider: String,
    pub model: String,
    pub content: Vec<ContentBlock>,
    pub finish: FinishReason,
    pub usage: Option<TokenUsage>,
    pub replay_state: Option<ReplayEnvelope>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub adapter_identity: Option<String>,
}
