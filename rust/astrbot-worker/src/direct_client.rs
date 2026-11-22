//! Cross-plugin service client with revocable direct-route caching and Host relay fallback.

use astrbot_wire::{
    peer::{Peer, PeerOwner},
    DirectInvoke, DirectRoute, ServiceRoute, WireError, DIRECT_ATTACH, DIRECT_INVOKE,
    METHOD_PLUGIN_SERVICE_INVOKE,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{Mutex, OnceCell};
use tokio_util::sync::CancellationToken;

const SERVICE_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_CACHED_ROUTES: usize = 64;

struct DirectConnection {
    route: DirectRoute,
    owner: PeerOwner,
}

enum CachedRoute {
    Direct(Arc<DirectConnection>),
    Relay { expires_at: u64 },
}

impl CachedRoute {
    fn valid(&self, now: u64) -> bool {
        match self {
            Self::Direct(conn) => {
                conn.route.grant.expires_at > now && !conn.owner.peer().is_closed()
            }
            Self::Relay { expires_at } => *expires_at > now,
        }
    }
}

#[derive(Default)]
pub(crate) struct DirectClients {
    entries: Mutex<BTreeMap<String, Arc<OnceCell<CachedRoute>>>>,
    generation: AtomicU64,
}

impl DirectClients {
    pub async fn close(&self) {
        let mut entries = self.entries.lock().await;
        self.generation.fetch_add(1, Ordering::AcqRel);
        for slot in entries.values() {
            if let Some(CachedRoute::Direct(conn)) = slot.get() {
                conn.owner.peer().close();
            }
        }
        entries.clear();
    }

    pub async fn invoke(
        &self,
        host: &Peer,
        parent_call_id: Option<&str>,
        target_plugin_id: &str,
        service: &str,
        input: Value,
        cancellation: &CancellationToken,
    ) -> Result<Value, WireError> {
        if host.is_closed() {
            return Err(WireError::host_unavailable("host connection closed"));
        }
        let key = format!("{target_plugin_id}/{service}");
        let direct = self
            .resolve(
                host,
                parent_call_id,
                &key,
                target_plugin_id,
                service,
                cancellation,
            )
            .await?;
        let now = unix_now()?;

        // Never start a potentially 30s direct call on a grant expiring soon; fall back to Host relay.
        match direct.filter(|conn| {
            conn.route.grant.expires_at > now.saturating_add(SERVICE_TIMEOUT.as_secs())
        }) {
            Some(conn) => {
                conn.owner
                    .peer()
                    .request_with_delivery(
                        DIRECT_INVOKE,
                        &DirectInvoke { input },
                        SERVICE_TIMEOUT,
                        cancellation,
                    )
                    .await
            }
            None => {
                host.request(
                    METHOD_PLUGIN_SERVICE_INVOKE,
                    &json!({
                        "target_plugin_id": target_plugin_id,
                        "service": service,
                        "input": input,
                        "parent_call_id": parent_call_id,
                    }),
                    SERVICE_TIMEOUT,
                    cancellation,
                )
                .await
            }
        }
    }

    async fn resolve(
        &self,
        host: &Peer,
        parent_call_id: Option<&str>,
        key: &str,
        target_plugin_id: &str,
        service: &str,
        cancellation: &CancellationToken,
    ) -> Result<Option<Arc<DirectConnection>>, WireError> {
        let now = unix_now()?;
        let generation = self.generation.load(Ordering::Acquire);
        let slot = {
            let mut entries = self.entries.lock().await;
            entries.retain(|_, slot| match slot.get() {
                Some(route) => route.valid(now),
                None => Arc::strong_count(slot) > 1,
            });
            if !entries.contains_key(key) && entries.len() >= MAX_CACHED_ROUTES {
                return Ok(None);
            }
            entries.entry(key.to_owned()).or_default().clone()
        };

        let route = slot
            .get_or_try_init(|| async {
                let result = host
                    .request(
                        METHOD_PLUGIN_SERVICE_INVOKE,
                        &json!({
                            "route_only": true,
                            "target_plugin_id": target_plugin_id,
                            "service": service,
                            "parent_call_id": parent_call_id,
                        }),
                        Duration::from_secs(5),
                        cancellation,
                    )
                    .await?;
                let parsed: ServiceRoute = serde_json::from_value(result)
                    .map_err(|err| WireError::invalid_request(err.to_string()))?;
                let Some(direct_route) = parsed.direct else {
                    return Ok(CachedRoute::Relay {
                        expires_at: now + 60,
                    });
                };
                #[cfg(unix)]
                {
                    let stream = tokio::time::timeout(
                        Duration::from_secs(5),
                        tokio::net::UnixStream::connect(&direct_route.socket_path),
                    )
                    .await
                    .map_err(|_| WireError::new("DEADLINE_EXCEEDED", "direct connect timed out"))?
                    .map_err(|err| WireError::host_unavailable(err.to_string()))?;
                    let (reader, writer) = stream.into_split();
                    let (owner, _incoming) = Peer::start(reader, writer, false);
                    owner
                        .peer()
                        .request(
                            DIRECT_ATTACH,
                            &direct_route.grant,
                            Duration::from_secs(5),
                            cancellation,
                        )
                        .await?;
                    Ok(CachedRoute::Direct(Arc::new(DirectConnection {
                        route: direct_route,
                        owner,
                    })))
                }
                #[cfg(not(unix))]
                {
                    let _ = direct_route;
                    Ok(CachedRoute::Relay {
                        expires_at: now + 60,
                    })
                }
            })
            .await?;

        if host.is_closed() || self.generation.load(Ordering::Acquire) != generation {
            if let CachedRoute::Direct(conn) = route {
                conn.owner.peer().close();
            }
            return Err(WireError::cancelled("direct route resolution retired"));
        }
        if cancellation.is_cancelled() {
            return Err(WireError::cancelled("service call cancelled"));
        }
        Ok(match route {
            CachedRoute::Direct(conn) => Some(conn.clone()),
            CachedRoute::Relay { .. } => None,
        })
    }
}

struct PluginClientInner {
    peer: Peer,
    parent_call_id: String,
    clients: Arc<DirectClients>,
    retired: Arc<AtomicBool>,
    cancellation: CancellationToken,
}

/// Request-scoped cross-plugin service client bound to an active invocation.
#[derive(Clone)]
pub struct PluginClient {
    inner: Option<Arc<PluginClientInner>>,
}

impl PluginClient {
    pub(crate) fn for_invocation(
        peer: Peer,
        parent_call_id: String,
        clients: Arc<DirectClients>,
        retired: Arc<AtomicBool>,
        cancellation: CancellationToken,
    ) -> Self {
        Self {
            inner: Some(Arc::new(PluginClientInner {
                peer,
                parent_call_id,
                clients,
                retired,
                cancellation,
            })),
        }
    }

    pub fn disabled() -> Self {
        Self { inner: None }
    }

    /// Call an exported service on `target_plugin_id` (requires declared dependency).
    pub async fn call_service(
        &self,
        target_plugin_id: &str,
        service: &str,
        input: Value,
    ) -> Result<Value, WireError> {
        let Some(inner) = &self.inner else {
            return Err(WireError::capability_denied(
                "plugin service client is unavailable in this context",
            ));
        };
        if inner.retired.load(Ordering::Acquire) {
            return Err(WireError::call_scope_retired(
                "plugin service call scope has retired",
            ));
        }
        inner
            .clients
            .invoke(
                &inner.peer,
                Some(&inner.parent_call_id),
                target_plugin_id,
                service,
                input,
                &inner.cancellation,
            )
            .await
    }
}

pub(crate) fn unix_now() -> Result<u64, WireError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .map_err(|err| WireError::runtime_error(err.to_string()))
}
