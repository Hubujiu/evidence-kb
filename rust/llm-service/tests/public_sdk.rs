use llm_service::{
    Adapter, AdapterFuture, Cancellation, ContentBlock, EventSink, FinishReason, LlmError,
    ModelInfo, ModelSource, ReplayEnvelope, Request, Role, Service, StreamEvent, TokenUsage,
};
use serde_json::json;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};
use std::time::Duration;
use tokio::sync::Notify;

struct IndependentProvider {
    calls: AtomicUsize,
    release: Notify,
    requests: Mutex<Vec<Request>>,
    fail: bool,
}

impl IndependentProvider {
    fn new(fail: bool) -> Self {
        Self {
            calls: AtomicUsize::new(0),
            release: Notify::new(),
            requests: Mutex::new(vec![]),
            fail,
        }
    }
}

impl Adapter for IndependentProvider {
    fn stream(&self, request: Request, sink: EventSink, cancel: Cancellation) -> AdapterFuture<'_> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.requests.lock().unwrap().push(request);
            sink.send(StreamEvent::TextDelta {
                index: 7,
                text: "first".into(),
            })
            .await?;
            if self.fail {
                return Err(LlmError::new(
                    "FIXTURE_PROVIDER_FAILURE",
                    "connection interrupted",
                ));
            }
            tokio::select! {
                _ = self.release.notified() => {},
                _ = cancel.cancelled() => return Err(LlmError::new("LLM_ABORTED", "cancelled")),
            }
            sink.send(StreamEvent::TextDelta {
                index: 7,
                text: " second".into(),
            })
            .await?;
            sink.send(StreamEvent::Usage {
                usage: TokenUsage {
                    input_tokens: 8,
                    output_tokens: 2,
                    total_tokens: Some(13),
                    cache_read_tokens: Some(3),
                    ..Default::default()
                },
            })
            .await?;
            sink.send(StreamEvent::Finish {
                reason: FinishReason::Stop,
                replay_state: Some(ReplayEnvelope {
                    response: json!({"native": "opaque"}),
                    blocks: Some(vec![json!("block")]),
                }),
            })
            .await
        })
    }
}

#[tokio::test(flavor = "current_thread")]
async fn public_stream_delivers_before_provider_completes_and_generate_uses_same_assembly() {
    let provider = Arc::new(IndependentProvider::new(false));
    let service = Service::default();
    service.register("fixture", provider.clone()).unwrap();
    let request = Request::text("fixture", "model", "question");
    let prepared = service.prepare("fixture", "model").await.unwrap();
    let mut stream = prepared.stream(request.clone()).unwrap();
    assert!(
        matches!(stream.next().await, Some(StreamEvent::TextDelta { text, .. }) if text == "first")
    );
    assert!(stream.response().is_none());
    provider.release.notify_one();
    while stream.next().await.is_some() {}
    let response = stream.response().unwrap();
    assert_eq!(
        response.content,
        vec![ContentBlock::Text {
            text: "first second".into()
        }]
    );
    assert_eq!(response.usage.as_ref().unwrap().input_tokens, 8);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
    provider.release.notify_one();
    let complete = service.complete(request).await.unwrap();
    assert_eq!(complete.content, response.content);
    assert_eq!(complete.usage, response.usage);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 2);
}

#[tokio::test(flavor = "current_thread")]
async fn provider_failure_is_one_terminal_event_with_unknown_usage_and_no_retry() {
    let provider = Arc::new(IndependentProvider::new(true));
    let service = Service::default();
    service.register("fixture", provider.clone()).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "question"))
        .unwrap();
    assert!(matches!(
        stream.next().await,
        Some(StreamEvent::TextDelta { .. })
    ));
    assert!(matches!(stream.next().await, Some(StreamEvent::Finish {
        reason: FinishReason::Error { failure }, .. }) if failure.code == "FIXTURE_PROVIDER_FAILURE"));
    assert!(stream.next().await.is_none());
    let response = stream.response().unwrap();
    assert!(response.usage.is_none());
    assert!(response.replay_state.is_none());
    assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn cancellation_interrupts_a_pending_provider_without_a_replayed_attempt() {
    let provider = Arc::new(IndependentProvider::new(false));
    let service = Service::default();
    service.register("fixture", provider.clone()).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "question"))
        .unwrap();
    stream.next().await.unwrap();
    stream.cancellation().cancel();
    assert!(matches!(
        stream.next().await,
        Some(StreamEvent::Finish {
            reason: FinishReason::Aborted { .. },
            ..
        })
    ));
    assert!(stream.next().await.is_none());
    assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn prepared_call_is_bound_to_registration_and_stale_disposal_cannot_remove_replacement() {
    let provider = Arc::new(IndependentProvider::new(false));
    let service = Service::default();
    let old = service.register("fixture", provider.clone()).unwrap();
    let prepared = service.prepare("fixture", "model").await.unwrap();
    service.unregister("fixture", old).unwrap();
    let replacement = service.register("fixture", provider.clone()).unwrap();
    assert_ne!(old, replacement);
    assert_eq!(
        service.unregister("fixture", old).unwrap_err().code,
        "LLM_BINDING_CHANGED"
    );
    let error = prepared
        .stream(Request::text("fixture", "model", "question"))
        .err()
        .unwrap();
    assert_eq!(error.code, "LLM_BINDING_CHANGED");
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
    assert_eq!(service.providers().unwrap(), vec!["fixture"]);
}

#[tokio::test(flavor = "current_thread")]
async fn an_admitted_call_keeps_its_adapter_alive_when_the_route_is_replaced() {
    let provider = Arc::new(IndependentProvider::new(false));
    let service = Service::default();
    let old = service.register("fixture", provider.clone()).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "question"))
        .unwrap();
    stream.next().await.unwrap();
    service.unregister("fixture", old).unwrap();
    service
        .register("fixture", Arc::new(IndependentProvider::new(true)))
        .unwrap();
    provider.release.notify_one();
    while stream.next().await.is_some() {}
    assert_eq!(stream.response().unwrap().finish, FinishReason::Stop);
}

#[tokio::test(flavor = "current_thread")]
async fn replay_metadata_requires_the_same_adapter_identity_not_just_the_same_provider_name() {
    let provider = Arc::new(IndependentProvider::new(false));
    let service = Service::default();
    let old = service.register("fixture", provider.clone()).unwrap();
    provider.release.notify_one();
    let response = service
        .complete(Request::text("fixture", "model", "first"))
        .await
        .unwrap();
    let mut next = Request::text("fixture", "model", "next");
    next.messages.insert(
        0,
        llm_service::Message {
            role: Role::Assistant,
            content: response.content,
            id: None,
            tool_call_id: None,
            source: Some(ModelSource {
                provider: "fixture".into(),
                model: "model".into(),
                replay_state: response.replay_state,
                adapter_identity: response.adapter_identity,
            }),
        },
    );
    provider.release.notify_one();
    service.complete(next.clone()).await.unwrap();
    assert!(
        provider.requests.lock().unwrap()[1].messages[0]
            .source
            .as_ref()
            .unwrap()
            .replay_state
            .is_some()
    );
    service.unregister("fixture", old).unwrap();
    service.register("fixture", provider.clone()).unwrap();
    provider.release.notify_one();
    service.complete(next).await.unwrap();
    assert!(
        provider.requests.lock().unwrap()[2].messages[0]
            .source
            .as_ref()
            .unwrap()
            .replay_state
            .is_none()
    );
}

#[tokio::test(flavor = "current_thread")]
async fn unsupported_options_fail_before_provider_dispatch() {
    let provider = Arc::new(IndependentProvider::new(false));
    let service = Service::default();
    service.register("fixture", provider.clone()).unwrap();
    let mut request = Request::text("fixture", "model", "question");
    request.temperature = Some(0.4);
    let error = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(request)
        .err()
        .unwrap();
    assert_eq!(error.code, "UNSUPPORTED_LLM_OPTION");
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
}

struct CompleteOnly;

impl Adapter for CompleteOnly {
    fn resolve_model(&self, provider: &str, model: &str) -> Result<ModelInfo, LlmError> {
        let capabilities = llm_service::Capabilities {
            streaming: false,
            ..Default::default()
        };
        Ok(ModelInfo {
            capabilities,
            ..ModelInfo::basic(provider, model)
        })
    }

    fn stream(&self, _: Request, _: EventSink, _: Cancellation) -> AdapterFuture<'_> {
        panic!("unsupported streaming must not dispatch")
    }
}

#[tokio::test(flavor = "current_thread")]
async fn complete_only_capabilities_do_not_fabricate_a_stream_from_a_buffered_result() {
    let service = Service::default();
    service.register("sync", Arc::new(CompleteOnly)).unwrap();
    let error = service
        .prepare("sync", "model")
        .await
        .unwrap()
        .stream(Request::text("sync", "model", "question"))
        .err()
        .unwrap();
    assert_eq!(error.code, "LLM_STREAM_UNSUPPORTED");
}

type Run = dyn Fn(Request, EventSink, Cancellation) -> AdapterFuture<'static> + Send + Sync;
struct CallbackProvider(Arc<Run>);

impl Adapter for CallbackProvider {
    fn stream(&self, request: Request, sink: EventSink, cancel: Cancellation) -> AdapterFuture<'_> {
        (self.0)(request, sink, cancel)
    }
}

#[tokio::test(flavor = "current_thread")]
async fn completion_waits_for_provider_cleanup_after_the_terminal_event() {
    let sent = Arc::new(Notify::new());
    let release = Arc::new(Notify::new());
    let cleaned = Arc::new(AtomicUsize::new(0));
    let provider = CallbackProvider(Arc::new({
        let sent = sent.clone();
        let release = release.clone();
        let cleaned = cleaned.clone();
        move |_, sink, _| {
            let sent = sent.clone();
            let release = release.clone();
            let cleaned = cleaned.clone();
            Box::pin(async move {
                sink.send(StreamEvent::Finish {
                    reason: FinishReason::Stop,
                    replay_state: None,
                })
                .await?;
                sent.notify_one();
                release.notified().await;
                cleaned.store(1, Ordering::SeqCst);
                Ok(())
            })
        }
    }));
    let service = Arc::new(Service::default());
    service.register("fixture", Arc::new(provider)).unwrap();
    let completed = tokio::spawn(async move {
        service
            .complete(Request::text("fixture", "model", "q"))
            .await
    });
    sent.notified().await;
    tokio::task::yield_now().await;
    assert!(!completed.is_finished());
    assert_eq!(cleaned.load(Ordering::SeqCst), 0);
    release.notify_one();
    assert_eq!(completed.await.unwrap().unwrap().finish, FinishReason::Stop);
    assert_eq!(cleaned.load(Ordering::SeqCst), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn close_reports_pending_cleanup_and_can_later_drain_without_aborting_the_owner() {
    let release = Arc::new(Notify::new());
    let provider = CallbackProvider(Arc::new({
        let release = release.clone();
        move |_, sink, _| {
            let release = release.clone();
            Box::pin(async move {
                sink.send(StreamEvent::Finish {
                    reason: FinishReason::Stop,
                    replay_state: None,
                })
                .await?;
                release.notified().await;
                Ok(())
            })
        }
    }));
    let service = Service::default();
    service.register("fixture", Arc::new(provider)).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "q"))
        .unwrap();
    stream.next().await.unwrap();
    assert_eq!(
        stream
            .close(Duration::from_millis(1))
            .await
            .unwrap_err()
            .code,
        "LLM_CLEANUP_PENDING"
    );
    release.notify_one();
    stream.close(Duration::from_secs(1)).await.unwrap();
}

struct MutableProvider {
    configuration: Mutex<String>,
}

impl Adapter for MutableProvider {
    fn stream(&self, _: Request, _: EventSink, _: Cancellation) -> AdapterFuture<'_> {
        panic!("the prepared dispatch must be used")
    }

    fn prepare_call(
        self: Arc<Self>,
        provider: String,
        model: String,
    ) -> llm_service::PrepareFuture {
        Box::pin(async move {
            let snapshot = self.configuration.lock().unwrap().clone();
            let mut info = ModelInfo::basic(&provider, &model);
            info.default_max_tokens = Some(128);
            info.capabilities.options.insert("reasoningEffort".into());
            info.reasoning = Some(llm_service::ModelReasoning {
                efforts: vec![llm_service::ReasoningEffort {
                    id: "balanced".into(),
                    name: "Balanced".into(),
                    description: None,
                }],
                default_effort: Some("balanced".into()),
            });
            Ok(llm_service::PreparedAdapter::new(
                info,
                move |request, sink, _| {
                    let snapshot = snapshot.clone();
                    Box::pin(async move {
                        assert_eq!(request.max_tokens, Some(128));
                        assert_eq!(request.reasoning_effort.as_deref(), Some("balanced"));
                        sink.send(StreamEvent::TextDelta {
                            index: 0,
                            text: snapshot,
                        })
                        .await?;
                        sink.send(StreamEvent::Finish {
                            reason: FinishReason::Stop,
                            replay_state: None,
                        })
                        .await
                    })
                },
            ))
        })
    }
}

#[tokio::test(flavor = "current_thread")]
async fn prepared_dispatch_freezes_adapter_configuration_and_same_model_defaults() {
    let provider = Arc::new(MutableProvider {
        configuration: Mutex::new("old endpoint".into()),
    });
    let service = Service::default();
    service.register("fixture", provider.clone()).unwrap();
    let prepared = service.prepare("fixture", "model").await.unwrap();
    *provider.configuration.lock().unwrap() = "new endpoint".into();
    let response = prepared
        .complete(Request::text("fixture", "model", "q"))
        .await
        .unwrap();
    assert_eq!(
        response.content,
        vec![ContentBlock::Text {
            text: "old endpoint".into()
        }]
    );
    let mut invalid = Request::text("fixture", "model", "q");
    invalid.reasoning_effort = Some("invented".into());
    assert_eq!(
        service.complete(invalid).await.unwrap_err().code,
        "UNSUPPORTED_LLM_REASONING_EFFORT"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn cloned_sinks_cannot_accept_an_event_after_a_blocked_terminal_send() {
    let queued = Arc::new(Notify::new());
    let late_rejected = Arc::new(AtomicUsize::new(0));
    let provider = CallbackProvider(Arc::new({
        let queued = queued.clone();
        let late_rejected = late_rejected.clone();
        move |_, sink, _| {
            let queued = queued.clone();
            let late_rejected = late_rejected.clone();
            Box::pin(async move {
                for _ in 0..16 {
                    sink.send(StreamEvent::TextDelta {
                        index: 0,
                        text: "x".into(),
                    })
                    .await?;
                }
                let terminal = sink.clone();
                let finish = tokio::spawn(async move {
                    terminal
                        .send(StreamEvent::Finish {
                            reason: FinishReason::Stop,
                            replay_state: None,
                        })
                        .await
                });
                tokio::task::yield_now().await;
                let late = tokio::spawn(async move {
                    sink.send(StreamEvent::Usage {
                        usage: TokenUsage {
                            input_tokens: 1,
                            output_tokens: 1,
                            ..Default::default()
                        },
                    })
                    .await
                });
                queued.notify_one();
                finish.await.unwrap()?;
                let error = late.await.unwrap().unwrap_err();
                assert_eq!(error.code, "LLM_STREAM_FINISHED");
                late_rejected.store(1, Ordering::SeqCst);
                Ok(())
            })
        }
    }));
    let service = Service::default();
    service.register("fixture", Arc::new(provider)).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "q"))
        .unwrap();
    queued.notified().await;
    let mut terminals = 0;
    while let Some(event) = stream.next().await {
        terminals += usize::from(matches!(event, StreamEvent::Finish { .. }));
    }
    stream.close(Duration::from_secs(1)).await.unwrap();
    assert_eq!(terminals, 1);
    assert_eq!(late_rejected.load(Ordering::SeqCst), 1);
    assert!(stream.response().unwrap().usage.is_none());
}

#[tokio::test(flavor = "current_thread")]
async fn cancel_keeps_observed_usage_while_the_consumer_has_not_read_the_queue() {
    let queued = Arc::new(Notify::new());
    let provider = CallbackProvider(Arc::new({
        let queued = queued.clone();
        move |_, sink, cancel| {
            let queued = queued.clone();
            Box::pin(async move {
                sink.send(StreamEvent::Usage {
                    usage: TokenUsage {
                        input_tokens: 12,
                        output_tokens: 3,
                        ..Default::default()
                    },
                })
                .await?;
                queued.notify_one();
                cancel.cancelled().await;
                Ok(())
            })
        }
    }));
    let service = Service::default();
    service.register("fixture", Arc::new(provider)).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "q"))
        .unwrap();
    queued.notified().await;
    stream.cancellation().cancel();
    stream.next().await.unwrap();
    stream.close(Duration::from_secs(1)).await.unwrap();
    assert_eq!(stream.response().unwrap().usage.unwrap().input_tokens, 12);
}

#[tokio::test(flavor = "current_thread")]
async fn cancellation_before_dispatch_does_not_invoke_the_provider() {
    let provider = Arc::new(IndependentProvider::new(false));
    let service = Service::default();
    service.register("fixture", provider.clone()).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "q"))
        .unwrap();
    stream.cancellation().cancel();
    stream.next().await.unwrap();
    stream.close(Duration::from_secs(1)).await.unwrap();
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn cancellation_releases_a_provider_blocked_on_a_full_event_queue() {
    let queued = Arc::new(Notify::new());
    let provider = CallbackProvider(Arc::new({
        let queued = queued.clone();
        move |_, sink, _| {
            let queued = queued.clone();
            Box::pin(async move {
                for _ in 0..16 {
                    sink.send(StreamEvent::TextDelta {
                        index: 0,
                        text: "x".into(),
                    })
                    .await?;
                }
                queued.notify_one();
                sink.send(StreamEvent::TextDelta {
                    index: 0,
                    text: "blocked".into(),
                })
                .await
            })
        }
    }));
    let service = Service::default();
    service.register("fixture", Arc::new(provider)).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "q"))
        .unwrap();
    queued.notified().await;
    stream.cancellation().cancel();
    stream.next().await.unwrap();
    stream.close(Duration::from_secs(1)).await.unwrap();
    assert!(matches!(
        stream.response().unwrap().finish,
        FinishReason::Aborted { .. }
    ));
}

#[tokio::test(flavor = "current_thread")]
async fn a_panicking_provider_has_no_successful_finish_or_executable_tool_calls() {
    let provider = CallbackProvider(Arc::new(|_, sink, _| {
        Box::pin(async move {
            sink.send(StreamEvent::ToolCallDelta {
                index: 0,
                id: "a".into(),
                name: Some("read".into()),
                arguments_delta: "{".into(),
            })
            .await?;
            panic!("synthetic adapter panic")
        })
    }));
    let service = Service::default();
    service.register("fixture", Arc::new(provider)).unwrap();
    let mut stream = service
        .prepare("fixture", "model")
        .await
        .unwrap()
        .stream(Request::text("fixture", "model", "q"))
        .unwrap();
    while stream.next().await.is_some() {}
    let response = stream.response().unwrap();
    assert!(matches!(response.finish, FinishReason::Error { .. }));
    assert!(response.content.is_empty());
    assert_eq!(
        stream.close(Duration::from_secs(1)).await.unwrap_err().code,
        "LLM_PROVIDER_TASK_FAILED"
    );
}
