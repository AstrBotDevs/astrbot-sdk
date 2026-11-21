//! Dynamic runtime tool catalog updates (`tools.invalidate` / `tools.discover`).

use astrbot_wire::{
    peer::Peer, DynamicToolSpec, ToolDiscoveryResult, WireError, METHOD_TOOLS_INVALIDATE,
};
use futures::future::BoxFuture;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio_util::sync::CancellationToken;

pub type DynamicToolHandler = Arc<
    dyn Fn(Value, crate::InvocationContext) -> BoxFuture<'static, Result<Value, WireError>>
        + Send
        + Sync,
>;

#[derive(Clone)]
pub struct DiscoveredTool {
    pub spec: DynamicToolSpec,
    pub handler: DynamicToolHandler,
}

/// Dynamic instance tool registry notifying the Host when tools change.
#[derive(Clone, Default)]
pub struct RuntimeTools {
    inner: Arc<RuntimeToolsInner>,
}

#[derive(Default)]
struct RuntimeToolsInner {
    version: AtomicU64,
    tools: Mutex<BTreeMap<String, DiscoveredTool>>,
    peer: Mutex<Option<Peer>>,
}

impl RuntimeTools {
    pub(crate) fn bind_peer(&self, peer: Peer) {
        if let Ok(mut slot) = self.inner.peer.lock() {
            *slot = Some(peer);
        }
    }

    /// Replace the local runtime tools without notifying the Host (used inside `tools.discover`).
    pub fn replace_local(&self, tools: Vec<DiscoveredTool>) -> Result<u64, WireError> {
        let mut next = BTreeMap::new();
        for tool in tools {
            if next.contains_key(&tool.spec.name) {
                return Err(WireError::invalid_request(format!(
                    "duplicate dynamic tool name: {}",
                    tool.spec.name
                )));
            }
            next.insert(tool.spec.name.clone(), tool);
        }
        let version = self.inner.version.fetch_add(1, Ordering::AcqRel) + 1;
        let mut guard = self
            .inner
            .tools
            .lock()
            .map_err(|_| WireError::runtime_error("runtime tools lock poisoned"))?;
        *guard = next;
        Ok(version)
    }

    /// Replace the instance's runtime tools and notify the Host to re-discover.
    pub async fn replace(&self, tools: Vec<DiscoveredTool>) -> Result<u64, WireError> {
        let version = self.replace_local(tools)?;
        let peer = self
            .inner
            .peer
            .lock()
            .map_err(|_| WireError::runtime_error("runtime tools peer lock poisoned"))?
            .clone();
        if let Some(peer) = peer {
            let _ = peer
                .request(
                    METHOD_TOOLS_INVALIDATE,
                    &json!({ "version": version }),
                    Duration::from_secs(5),
                    &CancellationToken::new(),
                )
                .await;
        }
        Ok(version)
    }

    pub fn snapshot(&self) -> Result<ToolDiscoveryResult, WireError> {
        let version = self.inner.version.load(Ordering::Acquire);
        let guard = self
            .inner
            .tools
            .lock()
            .map_err(|_| WireError::runtime_error("runtime tools lock poisoned"))?;
        Ok(ToolDiscoveryResult {
            version,
            tools: guard.values().map(|t| t.spec.clone()).collect(),
        })
    }

    pub(crate) fn get(&self, name: &str) -> Option<DynamicToolHandler> {
        self.inner
            .tools
            .lock()
            .ok()
            .and_then(|guard| guard.get(name).map(|t| t.handler.clone()))
    }
}
