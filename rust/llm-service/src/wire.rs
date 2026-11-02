use crate::{Assembler, Capabilities, FinishReason, LlmError, Request, StreamEvent, TokenUsage};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::{Map, Value, json};

fn decode<T: DeserializeOwned>(value: Value, code: &str) -> Result<T, LlmError> {
    serde_json::from_value(value).map_err(|error| LlmError::new(code, error.to_string()))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ValidateRequest {
    request: Request,
    capabilities: Capabilities,
    #[serde(default)]
    adapter_identity: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ApplyStream {
    state: Option<Assembler>,
    event: Option<Value>,
    provider: String,
    model: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct LegacyMessage {
    role: String,
    content_is_text: bool,
    fields: Vec<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct LegacyRequest {
    pub(crate) provider: String,
    pub(crate) model: String,
    messages: Vec<LegacyMessage>,
    max_tokens: Option<u64>,
    options: Vec<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct LegacyResponse {
    pub(crate) provider: String,
    pub(crate) model: String,
    pub(crate) expected_provider: String,
    pub(crate) expected_model: String,
    pub(crate) text_is_text: bool,
    pub(crate) finish_reason: Option<String>,
    pub(crate) usage: Option<Value>,
}

fn aggregate_usage(raw: Option<Value>) -> Option<TokenUsage> {
    let raw = raw?;
    let input = raw.get("prompt_tokens")?.as_u64()?;
    let output = raw.get("completion_tokens")?.as_u64()?;
    let cache_read = raw
        .get("prompt_tokens_details")
        .and_then(|v| v.get("cached_tokens"))
        .and_then(Value::as_u64);
    let cache_write = raw
        .get("prompt_tokens_details")
        .and_then(|v| v.get("cache_write_tokens"))
        .and_then(Value::as_u64);
    let uncached = input
        .checked_sub(cache_read.unwrap_or(0))?
        .checked_sub(cache_write.unwrap_or(0))?;
    let derived = input.checked_add(output)?;
    let total = match raw.get("total_tokens") {
        Some(value) => value.as_u64().filter(|v| *v >= derived),
        None => Some(derived),
    };
    Some(TokenUsage {
        input_tokens: uncached,
        output_tokens: output,
        total_tokens: total,
        cache_read_tokens: cache_read,
        cache_write_tokens: cache_write,
        reasoning_tokens: raw
            .get("completion_tokens_details")
            .and_then(|v| v.get("reasoning_tokens"))
            .and_then(Value::as_u64),
    })
}

pub fn dispatch(command: &str, mut object: Map<String, Value>) -> Result<Value, LlmError> {
    if object.remove("protocol").as_ref().and_then(Value::as_str) != Some(crate::PROTOCOL) {
        return Err(LlmError::new(
            "LLM_PROTOCOL_UNSUPPORTED",
            "expected llm-service/1",
        ));
    }
    match command {
        "llm_validate_request" => {
            let args: ValidateRequest = decode(Value::Object(object), "INVALID_LLM_REQUEST")?;
            args.request.validate(&args.capabilities)?;
            Ok(json!({"protocol": crate::PROTOCOL,
                "request": args.request.for_adapter(&args.adapter_identity)}))
        }
        "llm_stream_apply" => {
            let args: ApplyStream = decode(Value::Object(object), "INVALID_LLM_STREAM_STATE")?;
            let mut state = args.state.unwrap_or_default();
            state.validate_snapshot()?;
            if let Some(event) = args.event {
                state.push(decode::<StreamEvent>(event, "INVALID_LLM_EVENT")?)?;
            } else {
                state.end();
            }
            Ok(json!({"response": state.response(&args.provider, &args.model), "state": state}))
        }
        "llm_validate_legacy_chat" => {
            let args: LegacyRequest = decode(Value::Object(object), "INVALID_LLM_REQUEST")?;
            validate_legacy(&args)?;
            Ok(json!({"protocol": crate::PROTOCOL, "streaming": false}))
        }
        "llm_normalize_legacy_result" => {
            let args: LegacyResponse = decode(Value::Object(object), "INVALID_LLM_RESPONSE")?;
            normalize_legacy(args)
        }
        _ if ["llm_service_", "llm_route_", "llm_call_"]
            .iter()
            .any(|prefix| command.starts_with(prefix)) =>
        {
            crate::executor::dispatch(command.strip_prefix("llm_").unwrap_or(command), object)
        }
        _ => Err(LlmError::new("UNKNOWN_LLM_COMMAND", command)),
    }
}

pub(crate) fn validate_legacy(args: &LegacyRequest) -> Result<(), LlmError> {
    if args.provider.is_empty()
        || args.model.is_empty()
        || args.messages.is_empty()
        || args.max_tokens == Some(0)
    {
        return Err(LlmError::new(
            "INVALID_LLM_REQUEST",
            "invalid chat identity or limit",
        ));
    }
    if args
        .options
        .iter()
        .any(|key| !matches!(key.as_str(), "messages" | "max_tokens"))
    {
        return Err(LlmError::new(
            "UNSUPPORTED_LLM_OPTION",
            "legacy route accepts text and max_tokens only",
        ));
    }
    for message in &args.messages {
        if !message.content_is_text
            || !matches!(message.role.as_str(), "system" | "user" | "assistant")
        {
            return Err(LlmError::new(
                "INVALID_LLM_MESSAGE",
                "legacy route accepts text messages only",
            ));
        }
        if message
            .fields
            .iter()
            .any(|field| !matches!(field.as_str(), "role" | "content"))
        {
            return Err(LlmError::new(
                "UNSUPPORTED_LLM_OPTION",
                "legacy route cannot forward extra message fields",
            ));
        }
    }
    Ok(())
}

pub(crate) fn normalize_legacy(args: LegacyResponse) -> Result<Value, LlmError> {
    if args.provider != args.expected_provider
        || args.model != args.expected_model
        || !args.text_is_text
    {
        return Err(LlmError::new(
            "INVALID_LLM_RESPONSE",
            "response identity or text is invalid",
        ));
    }
    let finish = match args.finish_reason.as_deref() {
        Some("stop") => Some(FinishReason::Stop),
        Some("length" | "max-tokens" | "max_tokens") => Some(FinishReason::MaxTokens),
        Some("tool_calls" | "tool-calls") => Some(FinishReason::ToolCalls),
        Some(value) => Some(FinishReason::Error {
            failure: LlmError::new("UNSUPPORTED_LLM_FINISH", value),
        }),
        None => None,
    };
    Ok(json!({"protocol": crate::PROTOCOL, "usage": aggregate_usage(args.usage), "finish": finish}))
}
