//! Author context types enforcing instance, request, and direct-service privilege boundaries.

use crate::{direct_client::PluginClient, host::HostClient, state::RuntimeState, tasks::TaskGroup};
use astrbot_wire::{CapabilityGrant, WireError};
use serde_json::Value;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};
use tokio_util::sync::CancellationToken;

/// Instance lifecycle context passed to `on_start`.
#[derive(Clone)]
pub struct StartupContext {
    pub(crate) host: HostClient,
    pub(crate) runtime: Arc<RuntimeState>,
    /// Initial configuration snapshot at startup.
    pub config: Value,
    pub data_dir: Option<String>,
    pub tasks: TaskGroup,
    pub cancellation: CancellationToken,
}

impl StartupContext {
    /// Instance-scoped Host client valid until plugin shutdown.
    pub fn host(&self) -> &HostClient {
        &self.host
    }

    /// Read the latest confirmed configuration snapshot applied by the Host.
    pub fn current_config(&self) -> Result<Value, WireError> {
        self.runtime.config()
    }

    /// Read the currently granted capabilities.
    pub fn capabilities(&self) -> Result<Vec<CapabilityGrant>, WireError> {
        self.runtime.capabilities()
    }
}

/// Request-scoped context passed to commands, tools, messages, hooks, web routes, and hosted services.
#[derive(Clone)]
pub struct InvocationContext {
    pub call_id: String,
    pub(crate) host: HostClient,
    pub(crate) plugins: PluginClient,
    pub(crate) runtime: Arc<RuntimeState>,
    pub(crate) incoming: Option<Arc<IncomingStreamProxy>>,
    pub config: Value,
    pub cancellation: CancellationToken,
}

pub(crate) type StreamEmitItem = (
    u64,
    Value,
    bool,
    tokio::sync::oneshot::Sender<Result<(), WireError>>,
);

pub(crate) struct IncomingStreamProxy {
    seq: AtomicU64,
    tx: tokio::sync::mpsc::Sender<StreamEmitItem>,
}

impl IncomingStreamProxy {
    pub(crate) fn new(tx: tokio::sync::mpsc::Sender<StreamEmitItem>) -> Self {
        Self {
            seq: AtomicU64::new(1),
            tx,
        }
    }

    pub(crate) async fn emit(&self, value: Value, wait_for_ack: bool) -> Result<(), WireError> {
        let sequence = self.seq.fetch_add(1, Ordering::Relaxed);
        let (ack_tx, ack_rx) = tokio::sync::oneshot::channel();
        self.tx
            .send((sequence, value, wait_for_ack, ack_tx))
            .await
            .map_err(|_| WireError::cancelled("invocation stream closed"))?;
        ack_rx
            .await
            .map_err(|_| WireError::cancelled("invocation stream closed"))?
    }
}

impl InvocationContext {
    /// Request-scoped Host client; retires automatically when this handler finishes.
    pub fn host(&self) -> &HostClient {
        &self.host
    }

    /// Request-scoped cross-plugin service client; retires when this handler finishes.
    pub fn plugins(&self) -> &PluginClient {
        &self.plugins
    }

    /// Read the latest confirmed configuration snapshot.
    pub fn current_config(&self) -> Result<Value, WireError> {
        self.runtime.config()
    }

    /// Emit a backpressure-controlled yield/progress payload on this invocation.
    pub async fn yield_value(&self, value: Value, wait_for_ack: bool) -> Result<(), WireError> {
        let Some(proxy) = &self.incoming else {
            return Err(WireError::invalid_request(
                "yield_value is only available inside streaming invocations",
            ));
        };
        proxy.emit(value, wait_for_ack).await
    }

    /// Emit a progress notification payload on a resumable or streaming tool invocation.
    pub async fn progress(&self, payload: Value) -> Result<(), WireError> {
        self.yield_value(payload, true).await
    }
}

/// Direct-service context: exposes ONLY configuration and cancellation.
///
/// Intentionally carries NO `HostClient` and NO `PluginClient` privileges.
#[derive(Clone)]
pub struct DirectContext {
    pub config: Value,
    pub(crate) runtime: Arc<RuntimeState>,
    pub cancellation: CancellationToken,
}

impl DirectContext {
    /// Read the latest confirmed configuration snapshot.
    pub fn current_config(&self) -> Result<Value, WireError> {
        self.runtime.config()
    }
}
