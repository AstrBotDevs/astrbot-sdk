//! Scoped Host client & mock backend for unit testing Worker handlers offline.

use crate::state::RuntimeState;
use astrbot_wire::{
    peer::{Peer, StreamHandle},
    WireError, METHOD_CAPABILITY_INVOKE,
};
use futures::future::BoxFuture;
use serde_json::{json, Map, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio_util::sync::CancellationToken;

const DEFAULT_HOST_TIMEOUT: Duration = Duration::from_secs(30);

pub trait HostBackend: Send + Sync + 'static {
    fn invoke<'a>(
        &'a self,
        capability: &'a str,
        operation: &'a str,
        input: Value,
    ) -> BoxFuture<'a, Result<Value, WireError>>;
}

pub type MockHandlerFn = Arc<dyn Fn(Value) -> Result<Value, WireError> + Send + Sync>;

#[derive(Default)]
pub struct MockHost {
    handlers: std::sync::Mutex<std::collections::BTreeMap<(String, String), MockHandlerFn>>,
}

impl MockHost {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn on<F>(&self, capability: impl Into<String>, operation: impl Into<String>, handler: F)
    where
        F: Fn(Value) -> Result<Value, WireError> + Send + Sync + 'static,
    {
        if let Ok(mut map) = self.handlers.lock() {
            map.insert((capability.into(), operation.into()), Arc::new(handler));
        }
    }
}

impl HostBackend for MockHost {
    fn invoke<'a>(
        &'a self,
        capability: &'a str,
        operation: &'a str,
        input: Value,
    ) -> BoxFuture<'a, Result<Value, WireError>> {
        let found = self.handlers.lock().ok().and_then(|map| {
            map.get(&(capability.to_owned(), operation.to_owned()))
                .cloned()
        });
        Box::pin(async move {
            match found {
                Some(handler) => handler(input),
                None => Err(WireError::not_found(format!(
                    "mock host has no handler for {capability}/{operation}"
                ))),
            }
        })
    }
}

#[derive(Clone)]
enum HostInner {
    Peer {
        peer: Peer,
        parent_call_id: Option<String>,
        runtime: Arc<RuntimeState>,
        retired: Arc<AtomicBool>,
        cancellation: CancellationToken,
    },
    Mock {
        backend: Arc<dyn HostBackend>,
        retired: Arc<AtomicBool>,
    },
}

/// Scoped Host capability client.
///
/// Request-scoped clients automatically retire as soon as their owning invocation completes,
/// returning `CALL_SCOPE_RETIRED` on late calls. Instance-scoped clients retire on shutdown.
#[derive(Clone)]
pub struct HostClient {
    inner: HostInner,
}

impl HostClient {
    pub(crate) fn for_instance(
        peer: Peer,
        runtime: Arc<RuntimeState>,
        cancellation: CancellationToken,
    ) -> Self {
        Self {
            inner: HostInner::Peer {
                peer,
                parent_call_id: None,
                runtime,
                retired: Arc::new(AtomicBool::new(false)),
                cancellation,
            },
        }
    }

    pub(crate) fn for_invocation(
        peer: Peer,
        parent_call_id: String,
        runtime: Arc<RuntimeState>,
        retired: Arc<AtomicBool>,
        cancellation: CancellationToken,
    ) -> Self {
        Self {
            inner: HostInner::Peer {
                peer,
                parent_call_id: Some(parent_call_id),
                runtime,
                retired,
                cancellation,
            },
        }
    }

    pub fn from_mock(backend: Arc<dyn HostBackend>) -> Self {
        Self {
            inner: HostInner::Mock {
                backend,
                retired: Arc::new(AtomicBool::new(false)),
            },
        }
    }

    pub fn retire(&self) {
        match &self.inner {
            HostInner::Peer { retired, .. } | HostInner::Mock { retired, .. } => {
                retired.store(true, Ordering::Release);
            }
        }
    }

    pub fn is_retired(&self) -> bool {
        match &self.inner {
            HostInner::Peer {
                retired, runtime, ..
            } => retired.load(Ordering::Acquire) || runtime.shutting_down.load(Ordering::Acquire),
            HostInner::Mock { retired, .. } => retired.load(Ordering::Acquire),
        }
    }

    /// Invoke one Host capability operation (`capability.invoke`).
    pub async fn invoke(
        &self,
        capability: &str,
        operation: &str,
        input: Value,
    ) -> Result<Value, WireError> {
        if self.is_retired() {
            return Err(WireError::call_scope_retired(
                "host client call scope has retired",
            ));
        }
        match &self.inner {
            HostInner::Mock { backend, .. } => backend.invoke(capability, operation, input).await,
            HostInner::Peer {
                peer,
                parent_call_id,
                runtime,
                cancellation,
                ..
            } => {
                if !runtime.has_capability(capability)
                    && capability != "storage.kv"
                    && capability != "assets.transfer"
                {
                    return Err(WireError::capability_denied(format!(
                        "capability was not granted: {capability}"
                    )));
                }
                let mut params = Map::new();
                params.insert("capability".into(), Value::String(capability.to_owned()));
                params.insert("operation".into(), Value::String(operation.to_owned()));
                params.insert("input".into(), input);
                if let Some(parent_id) = parent_call_id {
                    params.insert("parent_call_id".into(), Value::String(parent_id.clone()));
                }
                peer.request(
                    METHOD_CAPABILITY_INVOKE,
                    &params,
                    DEFAULT_HOST_TIMEOUT,
                    cancellation,
                )
                .await
            }
        }
    }

    /// Invoke one streaming Host capability (`capability.invoke` with yield/ack).
    pub async fn invoke_stream(
        &self,
        capability: &str,
        operation: &str,
        input: Value,
    ) -> Result<StreamHandle, WireError> {
        if self.is_retired() {
            return Err(WireError::call_scope_retired(
                "host client call scope has retired",
            ));
        }
        match &self.inner {
            HostInner::Mock { .. } => Err(WireError::invalid_request(
                "mock host does not support capability streams",
            )),
            HostInner::Peer {
                peer,
                parent_call_id,
                runtime,
                ..
            } => {
                if !runtime.has_capability(capability)
                    && capability != "storage.kv"
                    && capability != "assets.transfer"
                {
                    return Err(WireError::capability_denied(format!(
                        "capability was not granted: {capability}"
                    )));
                }
                let mut params = Map::new();
                params.insert("capability".into(), Value::String(capability.to_owned()));
                params.insert("operation".into(), Value::String(operation.to_owned()));
                params.insert("input".into(), input);
                if let Some(parent_id) = parent_call_id {
                    params.insert("parent_call_id".into(), Value::String(parent_id.clone()));
                }
                peer.request_stream(METHOD_CAPABILITY_INVOKE, &params).await
            }
        }
    }

    pub async fn storage_get(&self, key: &str, default: Value) -> Result<Value, WireError> {
        let res = self
            .invoke("storage.kv", "get", json!({"key": key, "default": default}))
            .await?;
        Ok(res.get("value").cloned().unwrap_or(Value::Null))
    }

    pub async fn storage_set(&self, key: &str, value: Value) -> Result<(), WireError> {
        self.invoke("storage.kv", "set", json!({"key": key, "value": value}))
            .await?;
        Ok(())
    }

    pub async fn storage_delete(&self, key: &str) -> Result<(), WireError> {
        self.invoke("storage.kv", "delete", json!({"key": key}))
            .await?;
        Ok(())
    }

    pub async fn send_message(&self, umo: Value, message: Value) -> Result<Value, WireError> {
        self.invoke(
            "message.send",
            "send",
            json!({"umo": umo, "message": message}),
        )
        .await
    }

    pub async fn llm_generate(&self, input: Value) -> Result<Value, WireError> {
        self.invoke("llm.generate", "generate", input).await
    }
}
