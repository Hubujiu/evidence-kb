use crate::{
    BlockType, ContentBlock, FinishReason, LlmError, MAX_BLOCKS, MAX_CONTENT_BYTES, ReplayEnvelope,
    Response, StreamEvent, TokenUsage,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct PartialBlock {
    index: u32,
    kind: BlockType,
    text: String,
    id: String,
    name: String,
    arguments: String,
    closed: Option<ContentBlock>,
}

impl PartialBlock {
    fn block(&self) -> Option<ContentBlock> {
        if let Some(block) = &self.closed {
            return Some(block.clone());
        }
        Some(match self.kind {
            BlockType::Text => ContentBlock::Text {
                text: self.text.clone(),
            },
            BlockType::Reasoning => ContentBlock::Reasoning {
                text: self.text.clone(),
            },
            BlockType::ToolCall => ContentBlock::ToolCall {
                id: self.id.clone(),
                name: self.name.clone(),
                arguments: self.arguments.clone(),
            },
            BlockType::Image
            | BlockType::File
            | BlockType::ToolAddition
            | BlockType::ToolRemoval => return None,
        })
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Assembler {
    blocks: Vec<PartialBlock>,
    usage: Option<TokenUsage>,
    finish: Option<FinishReason>,
    replay: Option<ReplayEnvelope>,
}

impl Assembler {
    pub fn validate_snapshot(&self) -> Result<(), LlmError> {
        let mut indexes = std::collections::BTreeSet::new();
        if self.blocks.len() > MAX_BLOCKS
            || self.blocks.iter().any(|block| {
                block.index as usize >= MAX_BLOCKS
                    || !indexes.insert(block.index)
                    || !matches!(
                        block.kind,
                        BlockType::Text | BlockType::Reasoning | BlockType::ToolCall
                    )
                    || block
                        .closed
                        .as_ref()
                        .is_some_and(|closed| closed.kind() != block.kind)
            })
            || (self.finish.is_none() && self.replay.is_some())
            || serde_json::to_vec(self).map_or(true, |v| v.len() > MAX_CONTENT_BYTES)
        {
            return Err(LlmError::new(
                "INVALID_LLM_STREAM_STATE",
                "invalid assembler snapshot",
            ));
        }
        if let Some(usage) = &self.usage {
            usage.validate()?;
        }
        if let Some(finish) = &self.finish {
            self.validate_finish(finish)
                .map_err(|error| LlmError::new("INVALID_LLM_STREAM_STATE", error.message))?;
        }
        Ok(())
    }

    fn validate_finish(&self, reason: &FinishReason) -> Result<(), LlmError> {
        if matches!(reason, FinishReason::Stop | FinishReason::ToolCalls) {
            let mut ids = std::collections::BTreeSet::new();
            for block in &self.blocks {
                if let Some(ContentBlock::ToolCall {
                    id,
                    name,
                    arguments,
                }) = block.block()
                    && (id.is_empty()
                        || name.is_empty()
                        || !ids.insert(id)
                        || !serde_json::from_str::<Value>(&arguments).is_ok_and(|v| v.is_object()))
                {
                    return Err(LlmError::new(
                        "INCOMPLETE_LLM_TOOL_CALL",
                        "tool call is not executable",
                    ));
                }
            }
            if matches!(reason, FinishReason::ToolCalls) == ids.is_empty() {
                return Err(LlmError::new(
                    "INVALID_LLM_FINISH",
                    "finish reason disagrees with tool calls",
                ));
            }
        }
        Ok(())
    }

    fn ensure(&mut self, index: u32, kind: BlockType) -> Result<&mut PartialBlock, LlmError> {
        if index as usize >= MAX_BLOCKS
            || !matches!(
                kind,
                BlockType::Text | BlockType::Reasoning | BlockType::ToolCall
            )
        {
            return Err(LlmError::new(
                "UNSUPPORTED_LLM_OUTPUT_BLOCK",
                "unsupported output type or index",
            ));
        }
        let position = match self.blocks.iter().position(|p| p.index == index) {
            Some(position) => position,
            None => {
                if self.blocks.len() >= MAX_BLOCKS {
                    return Err(LlmError::new(
                        "LLM_STREAM_TOO_LARGE",
                        "too many output blocks",
                    ));
                }
                self.blocks.push(PartialBlock {
                    index,
                    kind: kind.clone(),
                    text: String::new(),
                    id: String::new(),
                    name: String::new(),
                    arguments: String::new(),
                    closed: None,
                });
                self.blocks.len() - 1
            }
        };
        let block = &mut self.blocks[position];
        if block.kind != kind || block.closed.is_some() {
            return Err(LlmError::new(
                "INVALID_LLM_BLOCK_TRANSITION",
                "closed block or changed block type",
            ));
        }
        Ok(block)
    }

    pub fn finished(&self) -> bool {
        self.finish.is_some()
    }

    pub fn push(&mut self, event: StreamEvent) -> Result<(), LlmError> {
        self.validate_snapshot()?;
        let mut next = self.clone();
        next.push_checked(event)?;
        *self = next;
        Ok(())
    }

    fn push_checked(&mut self, event: StreamEvent) -> Result<(), LlmError> {
        if self.finished() {
            return Err(LlmError::new(
                "LLM_STREAM_FINISHED",
                "events after terminal finish are forbidden",
            ));
        }
        match event {
            StreamEvent::BlockStart { index, block_type } => {
                if self.blocks.iter().any(|p| p.index == index) {
                    return Err(LlmError::new(
                        "INVALID_LLM_BLOCK_TRANSITION",
                        "duplicate block-start",
                    ));
                }
                self.ensure(index, block_type)?;
            }
            StreamEvent::TextDelta { index, text } => {
                self.ensure(index, BlockType::Text)?.text.push_str(&text)
            }
            StreamEvent::ReasoningDelta { index, text } => self
                .ensure(index, BlockType::Reasoning)?
                .text
                .push_str(&text),
            StreamEvent::ToolCallDelta {
                index,
                id,
                name,
                arguments_delta,
            } => {
                let block = self.ensure(index, BlockType::ToolCall)?;
                if id.is_empty()
                    || (!block.id.is_empty() && block.id != id)
                    || name
                        .as_ref()
                        .is_some_and(|n| !block.name.is_empty() && &block.name != n)
                {
                    return Err(LlmError::new(
                        "INVALID_LLM_TOOL_CALL",
                        "tool identity changed within a block",
                    ));
                }
                block.id = id;
                if let Some(name) = name {
                    block.name = name;
                }
                block.arguments.push_str(&arguments_delta);
            }
            StreamEvent::BlockEnd { index, block } => {
                let active = self.ensure(index, block.kind())?;
                if let ContentBlock::ToolCall { id, name, .. } = &block
                    && ((!active.id.is_empty() && active.id != *id)
                        || (!active.name.is_empty() && active.name != *name))
                {
                    return Err(LlmError::new(
                        "INVALID_LLM_TOOL_CALL",
                        "closing a block cannot change its tool identity",
                    ));
                }
                active.closed = Some(block);
            }
            StreamEvent::Usage { usage } => {
                usage.validate()?;
                self.usage = Some(usage);
            }
            StreamEvent::Finish {
                reason,
                replay_state,
            } => {
                self.validate_finish(&reason)?;
                self.replay = if reason.successful() {
                    replay_state
                } else {
                    None
                };
                self.finish = Some(reason);
            }
        }
        if serde_json::to_vec(self).map_or(true, |v| v.len() > MAX_CONTENT_BYTES) {
            return Err(LlmError::new(
                "LLM_STREAM_TOO_LARGE",
                "output exceeds protocol limit",
            ));
        }
        Ok(())
    }

    pub fn fail(&mut self, error: LlmError, aborted: bool) -> StreamEvent {
        let reason = if aborted {
            FinishReason::Aborted { failure: error }
        } else {
            FinishReason::Error { failure: error }
        };
        self.finish = Some(reason.clone());
        self.replay = None;
        StreamEvent::Finish {
            reason,
            replay_state: None,
        }
    }

    pub fn end(&mut self) {
        if !self.finished() {
            self.fail(
                LlmError::new(
                    "LLM_STREAM_MISSING_FINISH",
                    "provider ended without a terminal event",
                ),
                false,
            );
        }
    }

    pub fn response(&self, provider: &str, model: &str) -> Option<Response> {
        let finish = self.finish.clone()?;
        let all: Vec<_> = self.blocks.iter().filter_map(PartialBlock::block).collect();
        let kept: Vec<_> = all
            .iter()
            .map(|block| match finish {
                FinishReason::ToolCalls => true,
                FinishReason::Stop | FinishReason::MaxTokens => {
                    !matches!(block, ContentBlock::ToolCall { .. })
                }
                FinishReason::Error { .. } | FinishReason::Aborted { .. } => block.is_safe_text(),
            })
            .collect();
        let content = all
            .into_iter()
            .zip(&kept)
            .filter_map(|(block, keep)| keep.then_some(block))
            .collect();
        let replay_state = self.replay.as_ref().and_then(|envelope| {
            if let Some(blocks) = &envelope.blocks {
                if blocks.len() != kept.len() {
                    return None;
                }
                Some(ReplayEnvelope {
                    response: envelope.response.clone(),
                    blocks: Some(
                        blocks
                            .iter()
                            .zip(&kept)
                            .filter_map(|(block, keep)| keep.then_some(block.clone()))
                            .collect(),
                    ),
                })
            } else {
                Some(envelope.clone())
            }
        });
        Some(Response {
            provider: provider.into(),
            model: model.into(),
            content,
            finish,
            usage: self.usage.clone(),
            replay_state,
            adapter_identity: None,
        })
    }
}
