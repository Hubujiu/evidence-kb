use llm_service::{Assembler, BlockType, ContentBlock, FinishReason, ReplayEnvelope, StreamEvent};
use serde_json::{Value, json};

fn tool(index: u32, id: &str) -> StreamEvent {
    StreamEvent::ToolCallDelta {
        index,
        id: id.into(),
        name: Some("lookup".into()),
        arguments_delta: "{}".into(),
    }
}
fn finish(reason: FinishReason, blocks: Option<Vec<Value>>) -> StreamEvent {
    StreamEvent::Finish {
        reason,
        replay_state: Some(ReplayEnvelope {
            response: json!("response"),
            blocks,
        }),
    }
}
fn unchanged(a: &mut Assembler, event: StreamEvent) {
    let before = serde_json::to_value(&*a).unwrap();
    assert!(a.push(event).is_err());
    assert_eq!(serde_json::to_value(a).unwrap(), before);
}
#[test]
fn sparse_arrival_order_and_distinct_tool_ids_are_preserved() {
    let mut a = Assembler::default();
    a.push(tool(7, "second")).unwrap();
    a.push(tool(2, "first")).unwrap();
    a.push(finish(
        FinishReason::ToolCalls,
        Some(vec![json!(7), json!(2)]),
    ))
    .unwrap();
    let r = a.response("p", "m").unwrap();
    assert!(matches!(&r.content[0],ContentBlock::ToolCall {id,..} if id=="second"));
    assert!(matches!(&r.content[1],ContentBlock::ToolCall {id,..} if id=="first"));
    assert_eq!(
        r.replay_state.unwrap().blocks.unwrap(),
        vec![json!(7), json!(2)]
    );
    unchanged(
        &mut a,
        StreamEvent::TextDelta {
            index: 3,
            text: "late".into(),
        },
    );
}
#[test]
fn duplicate_ids_and_invalid_finish_are_transactional() {
    let mut a = Assembler::default();
    a.push(tool(7, "same")).unwrap();
    a.push(tool(2, "same")).unwrap();
    unchanged(&mut a, finish(FinishReason::ToolCalls, None));
    unchanged(&mut a, finish(FinishReason::Stop, None));
    a.push(finish(FinishReason::MaxTokens, None)).unwrap();
    assert!(a.response("p", "m").unwrap().content.is_empty());
}
#[test]
fn duplicate_start_type_change_identity_change_and_closed_delta_are_transactional() {
    let mut a = Assembler::default();
    a.push(tool(7, "identity")).unwrap();
    unchanged(
        &mut a,
        StreamEvent::BlockStart {
            index: 7,
            block_type: BlockType::ToolCall,
        },
    );
    unchanged(
        &mut a,
        StreamEvent::TextDelta {
            index: 7,
            text: "wrong type".into(),
        },
    );
    unchanged(&mut a, tool(7, "different"));
    unchanged(
        &mut a,
        StreamEvent::BlockEnd {
            index: 7,
            block: ContentBlock::ToolCall {
                id: "different".into(),
                name: "lookup".into(),
                arguments: "{}".into(),
            },
        },
    );
    a.push(StreamEvent::BlockEnd {
        index: 7,
        block: ContentBlock::ToolCall {
            id: "identity".into(),
            name: "lookup".into(),
            arguments: "{}".into(),
        },
    })
    .unwrap();
    unchanged(&mut a, tool(7, "identity"));
    a.push(finish(FinishReason::ToolCalls, None)).unwrap();
}
#[test]
fn max_tokens_projects_replay_with_the_same_kept_mask() {
    let mut a = Assembler::default();
    a.push(tool(7, "hidden")).unwrap();
    a.push(StreamEvent::TextDelta {
        index: 2,
        text: "safe".into(),
    })
    .unwrap();
    a.push(finish(
        FinishReason::MaxTokens,
        Some(vec![json!("tool"), json!("text")]),
    ))
    .unwrap();
    let r = a.response("p", "m").unwrap();
    assert_eq!(r.content.len(), 1);
    assert_eq!(r.replay_state.unwrap().blocks.unwrap(), vec![json!("text")]);
}
#[test]
fn mismatched_replay_length_is_dropped_not_silently_zipped() {
    for blocks in [vec![], vec![json!(1), json!(2)]] {
        let mut a = Assembler::default();
        a.push(StreamEvent::TextDelta {
            index: 7,
            text: "safe".into(),
        })
        .unwrap();
        a.push(finish(FinishReason::Stop, Some(blocks))).unwrap();
        assert!(a.response("p", "m").unwrap().replay_state.is_none());
    }
}
#[test]
fn failure_keeps_safe_text_but_never_replay_or_partial_tools() {
    let mut a = Assembler::default();
    a.push(tool(7, "hidden")).unwrap();
    a.push(StreamEvent::ReasoningDelta {
        index: 2,
        text: "private".into(),
    })
    .unwrap();
    a.push(StreamEvent::TextDelta {
        index: 3,
        text: "safe".into(),
    })
    .unwrap();
    a.fail(llm_service::LlmError::new("TEST", "synthetic"), false);
    let r = a.response("p", "m").unwrap();
    assert_eq!(r.content.len(), 2);
    assert!(r.replay_state.is_none());
    assert!(matches!(&r.content[0],ContentBlock::Reasoning {text} if text=="private"));
    assert!(matches!(&r.content[1],ContentBlock::Text {text} if text=="safe"));
}
