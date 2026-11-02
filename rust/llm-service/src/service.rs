use crate::{
    Assembler, FinishReason, LlmError, ModelInfo, Prepared, Registry, Request, Response,
    StreamEvent, TokenUsage,
};
use std::future::Future;
use std::pin::Pin;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use tokio::sync::{Notify, mpsc};

pub type AdapterFuture<'a> = Pin<Box<dyn Future<Output = Result<(), LlmError>> + Send + 'a>>;
pub type PrepareFuture = Pin<Box<dyn Future<Output = Result<PreparedAdapter, LlmError>> + Send>>;
type PreparedDispatch =
    dyn Fn(Request, EventSink, Cancellation) -> AdapterFuture<'static> + Send + Sync;

pub struct PreparedAdapter {
    info: ModelInfo,
    dispatch: Arc<PreparedDispatch>,
}

impl PreparedAdapter {
    pub fn new<F>(info: ModelInfo, dispatch: F) -> Self
    where
        F: Fn(Request, EventSink, Cancellation) -> AdapterFuture<'static> + Send + Sync + 'static,
    {
        Self {
            info,
            dispatch: Arc::new(dispatch),
        }
    }

    pub fn info(&self) -> &ModelInfo {
        &self.info
    }
}

#[derive(Clone, Default)]
pub struct Cancellation {
    flag: Arc<AtomicBool>,
    notify: Arc<Notify>,
}

impl Cancellation {
    pub fn cancel(&self) {
        self.flag.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }
    pub fn is_cancelled(&self) -> bool {
        self.flag.load(Ordering::SeqCst)
    }
    pub async fn cancelled(&self) {
        loop {
            let pending = self.notify.notified();
            tokio::pin!(pending);
            pending.as_mut().enable();
            if self.is_cancelled() {
                return;
            }
            pending.await;
        }
    }
}

#[derive(Clone)]
pub struct EventSink {
    sender: mpsc::Sender<StreamEvent>,
    finished: Arc<AtomicBool>,
    cancellation: Cancellation,
    serial: Arc<tokio::sync::Mutex<()>>,
    observed_usage: Arc<Mutex<Option<TokenUsage>>>,
}

impl EventSink {
    pub async fn send(&self, event: StreamEvent) -> Result<(), LlmError> {
        let _serial = self.serial.lock().await;
        if self.finished.load(Ordering::SeqCst) {
            return Err(LlmError::new(
                "LLM_STREAM_FINISHED",
                "provider emitted after finish",
            ));
        }
        if serde_json::to_vec(&event).map_or(true, |v| v.len() > crate::MAX_CONTENT_BYTES) {
            return Err(LlmError::new(
                "LLM_STREAM_TOO_LARGE",
                "event exceeds protocol limit",
            ));
        }
        if let StreamEvent::Usage { usage } = &event {
            usage.validate()?;
            *self
                .observed_usage
                .lock()
                .map_err(|_| LlmError::new("LLM_SERVICE_UNAVAILABLE", "usage lock poisoned"))? =
                Some(usage.clone());
        }
        let terminal = matches!(event, StreamEvent::Finish { .. });
        tokio::select! {
            biased;
            _ = self.cancellation.cancelled() => Err(LlmError::new("LLM_ABORTED", "call cancelled")),
            result = self.sender.send(event) => {
                result.map_err(|_| LlmError::new("LLM_CONSUMER_CLOSED", "consumer closed the stream"))?;
                if terminal { self.finished.store(true, Ordering::SeqCst); }
                Ok(())
            }
        }
    }
}

pub trait Adapter: Send + Sync + 'static {
    fn stream(
        &self,
        request: Request,
        sink: EventSink,
        cancellation: Cancellation,
    ) -> AdapterFuture<'_>;

    fn provider_name(&self, provider: &str) -> String {
        provider.into()
    }

    fn list_models(&self, _provider: &str) -> Vec<ModelInfo> {
        vec![]
    }

    fn resolve_model(&self, provider: &str, model: &str) -> Result<ModelInfo, LlmError> {
        Ok(ModelInfo::basic(provider, model))
    }

    fn prepare_call(self: Arc<Self>, provider: String, model: String) -> PrepareFuture {
        Box::pin(async move {
            let info = self.resolve_model(&provider, &model)?;
            Ok(PreparedAdapter::new(
                info,
                move |request, sink, cancellation| {
                    let adapter = self.clone();
                    Box::pin(async move { adapter.stream(request, sink, cancellation).await })
                },
            ))
        })
    }
}

#[derive(Default)]
pub struct Service {
    routes: Registry<dyn Adapter>,
}

impl Service {
    pub fn register(&self, provider: &str, adapter: Arc<dyn Adapter>) -> Result<u64, LlmError> {
        Ok(self.routes.register(provider, adapter, None)?.generation)
    }

    pub fn unregister(&self, provider: &str, generation: u64) -> Result<(), LlmError> {
        self.routes.detach(provider, generation)
    }

    pub fn providers(&self) -> Result<Vec<String>, LlmError> {
        let mut names: Vec<_> = self
            .routes
            .inspect()?
            .into_iter()
            .map(|row| row.name)
            .collect();
        names.sort();
        Ok(names)
    }

    pub async fn prepare(&self, provider: &str, model: &str) -> Result<PreparedCall, LlmError> {
        let registration = self.routes.prepare(provider, None)?;
        let prepared = registration
            .snapshot()
            .payload
            .clone()
            .prepare_call(provider.into(), model.into())
            .await?;
        let info = prepared.info();
        registration.ensure_current()?;
        if info.provider != provider || info.id != model {
            return Err(LlmError::new(
                "INVALID_LLM_MODEL_IDENTITY",
                "adapter resolved a different route",
            ));
        }
        let identity = registration.snapshot().binding.replay_identity();
        Ok(PreparedCall {
            registration,
            prepared,
            identity,
        })
    }

    pub async fn complete(&self, request: Request) -> Result<Response, LlmError> {
        self.prepare(&request.provider, &request.model)
            .await?
            .complete(request)
            .await
    }
}

pub struct PreparedCall {
    registration: Prepared<dyn Adapter>,
    prepared: PreparedAdapter,
    identity: String,
}

impl PreparedCall {
    pub fn info(&self) -> &ModelInfo {
        self.prepared.info()
    }

    pub fn stream(self, mut request: Request) -> Result<LlmStream, LlmError> {
        if request.provider != self.info().provider || request.model != self.info().id {
            return Err(LlmError::new(
                "LLM_PREPARED_ROUTE_MISMATCH",
                "prepared route cannot be changed",
            ));
        }
        self.info().apply_defaults(&mut request)?;
        request.validate(&self.info().capabilities)?;
        if !self.info().capabilities.streaming {
            return Err(LlmError::new(
                "LLM_STREAM_UNSUPPORTED",
                "complete-only route cannot provide a stream",
            ));
        }
        self.registration.ensure_current()?;
        let runtime = tokio::runtime::Handle::try_current().map_err(|_| {
            LlmError::new("LLM_RUNTIME_REQUIRED", "stream requires a Tokio runtime")
        })?;
        let cancellation = Cancellation::default();
        let observed_usage = Arc::new(Mutex::new(None));
        let (sender, receiver) = mpsc::channel(16);
        let sink = EventSink {
            sender,
            finished: Arc::new(AtomicBool::new(false)),
            cancellation: cancellation.clone(),
            serial: Arc::new(tokio::sync::Mutex::new(())),
            observed_usage: observed_usage.clone(),
        };
        let provider = request.provider.clone();
        let model = request.model.clone();
        let identity = self.identity.clone();
        let call_cancellation = cancellation.clone();
        let owner = self.registration.admit()?;
        let task = runtime.spawn(async move {
            if call_cancellation.is_cancelled() {
                return Ok(());
            }
            let result = (self.prepared.dispatch)(
                request.for_adapter(&self.identity),
                sink.clone(),
                call_cancellation.clone(),
            )
            .await;
            drop(owner);
            if !sink.finished.load(Ordering::SeqCst) {
                let failure = result.err().unwrap_or_else(|| {
                    LlmError::new("LLM_STREAM_MISSING_FINISH", "provider ended without finish")
                });
                let reason = if call_cancellation.is_cancelled() {
                    FinishReason::Aborted { failure }
                } else {
                    FinishReason::Error { failure }
                };
                let _ = sink
                    .send(StreamEvent::Finish {
                        reason,
                        replay_state: None,
                    })
                    .await;
                Ok(())
            } else {
                result
            }
        });
        Ok(LlmStream {
            receiver,
            cancellation,
            task: Some(task),
            assembler: Assembler::default(),
            provider,
            model,
            identity,
            done: false,
            cleanup_error: None,
            observed_usage,
        })
    }

    pub async fn complete(self, request: Request) -> Result<Response, LlmError> {
        let mut stream = self.stream(request)?;
        while stream.next().await.is_some() {}
        stream.close(Duration::from_secs(5)).await?;
        stream
            .response()
            .ok_or_else(|| LlmError::new("LLM_STREAM_MISSING_FINISH", "missing response"))
    }
}

pub struct LlmStream {
    receiver: mpsc::Receiver<StreamEvent>,
    cancellation: Cancellation,
    task: Option<tokio::task::JoinHandle<Result<(), LlmError>>>,
    assembler: Assembler,
    provider: String,
    model: String,
    identity: String,
    done: bool,
    cleanup_error: Option<LlmError>,
    observed_usage: Arc<Mutex<Option<TokenUsage>>>,
}

impl LlmStream {
    pub fn cancellation(&self) -> Cancellation {
        self.cancellation.clone()
    }

    pub async fn next(&mut self) -> Option<StreamEvent> {
        if self.done {
            return None;
        }
        let event = tokio::select! {
            biased;
            _ = self.cancellation.cancelled() => StreamEvent::Finish {
                reason: FinishReason::Aborted { failure: LlmError::new("LLM_ABORTED", "call cancelled") }, replay_state: None },
            event = self.receiver.recv() => event.unwrap_or_else(|| StreamEvent::Finish {
                reason: FinishReason::Error { failure: LlmError::new("LLM_STREAM_MISSING_FINISH", "provider stream ended") }, replay_state: None }),
        };
        if matches!(event, StreamEvent::Finish { .. })
            && let Ok(observed) = self.observed_usage.lock()
            && let Some(usage) = observed.clone()
        {
            let _ = self.assembler.push(StreamEvent::Usage { usage });
        }
        let event = match self.assembler.push(event.clone()) {
            Ok(()) => event,
            Err(error) => {
                self.cancellation.cancel();
                self.assembler.fail(error, false)
            }
        };
        if self.assembler.finished() {
            self.done = true;
            self.receiver.close();
        }
        Some(event)
    }

    pub async fn close(&mut self, timeout: Duration) -> Result<(), LlmError> {
        if !self.done {
            self.cancellation.cancel();
            self.receiver.close();
        }
        if let Some(task) = self.task.as_mut() {
            let outcome = tokio::time::timeout(timeout, task).await.map_err(|_| {
                LlmError::new(
                    "LLM_CLEANUP_PENDING",
                    "provider has not completed cleanup; its task retains ownership",
                )
            })?;
            self.task = None;
            self.cleanup_error = match outcome {
                Ok(result) => result.err(),
                Err(error) => Some(LlmError::new("LLM_PROVIDER_TASK_FAILED", error.to_string())),
            };
        }
        self.cleanup_error.clone().map_or(Ok(()), Err)
    }

    pub fn response(&self) -> Option<Response> {
        self.assembler
            .response(&self.provider, &self.model)
            .map(|mut response| {
                response.adapter_identity = Some(self.identity.clone());
                response
            })
    }
}

impl Drop for LlmStream {
    fn drop(&mut self) {
        if !self.done {
            self.cancellation.cancel();
        }
    }
}
