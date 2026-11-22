//! Provider-side revocable Unix socket server for direct plugin service calls.
//! Direct handlers receive `DirectContext` with NO HostClient or PluginClient privileges.

use crate::{context::DirectContext, direct_client::unix_now, state::RuntimeState, ServiceHandler};
use astrbot_wire::{
    peer::{Incoming, Peer, PeerOwner},
    DirectGrant, DirectInvoke, DirectListen, DirectRoute, ServiceMode, WireError, DIRECT_ATTACH,
    DIRECT_INVOKE,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use subtle::ConstantTimeEq;
use tokio::sync::{mpsc, Semaphore};
use tokio_util::sync::CancellationToken;

struct GrantEntry {
    value: DirectGrant,
    cancelled: CancellationToken,
    connections: Arc<Semaphore>,
}

#[cfg(unix)]
struct SocketGuard {
    path: PathBuf,
    device: u64,
    inode: u64,
}

#[cfg(unix)]
impl Drop for SocketGuard {
    fn drop(&mut self) {
        use std::os::unix::fs::MetadataExt;
        if let Ok(meta) = self.path.symlink_metadata() {
            if meta.dev() == self.device && meta.ino() == self.inode {
                let _ = std::fs::remove_file(&self.path);
            }
        }
    }
}

pub(crate) struct DirectServer {
    options: DirectListen,
    grants: Arc<Mutex<BTreeMap<String, Arc<GrantEntry>>>>,
    stop: CancellationToken,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl DirectServer {
    #[cfg(unix)]
    pub fn bind(
        options: DirectListen,
        plugin_id: &str,
        services: Arc<BTreeMap<String, (ServiceMode, ServiceHandler)>>,
        runtime: Arc<RuntimeState>,
    ) -> Result<Self, WireError> {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let path = PathBuf::from(&options.socket_path);
        if options.provider_id != plugin_id
            || options.provider_generation == 0
            || !path.is_absolute()
            || options.socket_path.len() >= 104
        {
            return Err(WireError::invalid_request(
                "invalid host-assigned direct service socket path",
            ));
        }
        let parent_meta = path
            .parent()
            .ok_or_else(|| WireError::invalid_request("direct socket parent missing"))?
            .symlink_metadata()
            .map_err(|err| WireError::host_unavailable(err.to_string()))?;
        if !parent_meta.is_dir() || parent_meta.mode() & 0o077 != 0 {
            return Err(WireError::invalid_request(
                "direct socket directory must be private (0700)",
            ));
        }

        let listener = tokio::net::UnixListener::bind(&path)
            .map_err(|err| WireError::host_unavailable(err.to_string()))?;
        let sock_meta = path
            .symlink_metadata()
            .map_err(|err| WireError::host_unavailable(err.to_string()))?;
        let guard = SocketGuard {
            path,
            device: sock_meta.dev(),
            inode: sock_meta.ino(),
        };
        std::fs::set_permissions(&guard.path, std::fs::Permissions::from_mode(0o600))
            .map_err(|err| WireError::host_unavailable(err.to_string()))?;

        let grants = Arc::new(Mutex::new(BTreeMap::new()));
        let grants_clone = grants.clone();
        let stop = CancellationToken::new();
        let stopped = stop.clone();

        let task = tokio::spawn(async move {
            let _guard = guard;
            let mut connections = tokio::task::JoinSet::new();
            loop {
                tokio::select! {
                    biased;
                    _ = stopped.cancelled() => break,
                    res = listener.accept() => match res {
                        Ok((stream, _)) if connections.len() < 64 => {
                            let (reader, writer) = stream.into_split();
                            let (owner, incoming) = Peer::start(reader, writer, true);
                            let g = grants_clone.clone();
                            let s = services.clone();
                            let r = runtime.clone();
                            let c = stopped.clone();
                            connections.spawn(async move {
                                if let Err(err) = serve_connection(owner, incoming, g, s, r, c).await {
                                    tracing::debug!(code = %err.code, "direct service connection closed");
                                }
                            });
                        }
                        Ok(_) => {}
                        Err(err) => {
                            tracing::warn!(%err, "direct service listener failed");
                            break;
                        }
                    },
                    res = connections.join_next(), if !connections.is_empty() => {
                        if let Some(Err(err)) = res {
                            tracing::warn!(%err, "direct connection task failed");
                        }
                    }
                }
            }
            stopped.cancel();
            while let Some(res) = connections.join_next().await {
                if let Err(err) = res {
                    tracing::warn!(%err, "direct connection shutdown error");
                }
            }
        });

        Ok(Self {
            options,
            grants,
            stop,
            task: Some(task),
        })
    }

    #[cfg(not(unix))]
    pub fn bind(
        _options: DirectListen,
        _plugin_id: &str,
        _services: Arc<BTreeMap<String, (ServiceMode, ServiceHandler)>>,
        _runtime: Arc<RuntimeState>,
    ) -> Result<Self, WireError> {
        Err(WireError::new(
            "UNSUPPORTED_PLATFORM",
            "direct services require Unix sockets",
        ))
    }

    pub async fn install(
        &self,
        grant: DirectGrant,
        services: &BTreeMap<String, (ServiceMode, ServiceHandler)>,
    ) -> Result<DirectRoute, WireError> {
        let now = unix_now()?;
        if grant.provider_id != self.options.provider_id
            || grant.provider_generation != self.options.provider_generation
            || grant.credential.len() != 64
            || grant.id.is_empty()
            || grant.expires_at <= now
            || !services
                .get(&grant.handler_id)
                .is_some_and(|(mode, _)| *mode == ServiceMode::Direct)
        {
            return Err(WireError::new(
                "INVALID_GRANT",
                "grant does not target an active direct service",
            ));
        }

        let expired: Vec<String> = self
            .grants
            .lock()
            .map_err(|_| WireError::runtime_error("direct grant table poisoned"))?
            .iter()
            .filter(|(_, entry)| entry.value.expires_at <= now)
            .map(|(id, _)| id.clone())
            .collect();
        for id in expired {
            self.revoke(&id).await?;
        }

        let mut entries = self
            .grants
            .lock()
            .map_err(|_| WireError::runtime_error("direct grant table poisoned"))?;
        if entries.contains_key(&grant.id) || entries.len() >= 128 {
            return Err(WireError::conflict(
                "duplicate direct grant or grant capacity reached",
            ));
        }
        entries.insert(
            grant.id.clone(),
            Arc::new(GrantEntry {
                value: grant.clone(),
                cancelled: CancellationToken::new(),
                connections: Arc::new(Semaphore::new(64)),
            }),
        );
        Ok(DirectRoute {
            socket_path: self.options.socket_path.clone(),
            grant,
        })
    }

    pub async fn revoke(&self, id: &str) -> Result<(), WireError> {
        let entry = self
            .grants
            .lock()
            .map_err(|_| WireError::runtime_error("direct grant table poisoned"))?
            .remove(id);
        if let Some(entry) = entry {
            entry.cancelled.cancel();
            let _ = entry
                .connections
                .clone()
                .acquire_many_owned(64)
                .await
                .map_err(|_| WireError::runtime_error("direct grant drain barrier failed"))?;
        }
        Ok(())
    }

    pub async fn shutdown(&mut self) {
        self.stop.cancel();
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
        if let Ok(mut grants) = self.grants.lock() {
            grants.clear();
        }
    }
}

impl Drop for DirectServer {
    fn drop(&mut self) {
        self.stop.cancel();
    }
}

async fn serve_connection(
    owner: PeerOwner,
    mut incoming: mpsc::Receiver<Incoming>,
    grants: Arc<Mutex<BTreeMap<String, Arc<GrantEntry>>>>,
    services: Arc<BTreeMap<String, (ServiceMode, ServiceHandler)>>,
    runtime: Arc<RuntimeState>,
    stop: CancellationToken,
) -> Result<(), WireError> {
    let auth_req = tokio::select! {
        _ = stop.cancelled() => return Ok(()),
        req = tokio::time::timeout(Duration::from_secs(5), incoming.recv()) => {
            req.map_err(|_| WireError::new("DEADLINE_EXCEEDED", "direct attach timed out"))?
                .ok_or_else(|| WireError::host_unavailable("direct connection closed before attach"))?
        }
    };
    if auth_req.method != DIRECT_ATTACH {
        auth_req
            .respond(Err(WireError::invalid_request("expected direct.attach")))
            .await?;
        return Err(WireError::invalid_request("expected direct.attach"));
    }

    let supplied: DirectGrant = serde_json::from_value(Value::Object(auth_req.params.clone()))
        .map_err(|err| WireError::invalid_request(err.to_string()))?;
    let entry = {
        let map = grants
            .lock()
            .map_err(|_| WireError::runtime_error("direct grant table poisoned"))?;
        map.get(&supplied.id)
            .filter(|e| {
                let actual = &e.value;
                bool::from(
                    actual
                        .credential
                        .as_bytes()
                        .ct_eq(supplied.credential.as_bytes()),
                ) && actual.caller_id == supplied.caller_id
                    && actual.caller_generation == supplied.caller_generation
                    && actual.provider_id == supplied.provider_id
                    && actual.provider_generation == supplied.provider_generation
                    && actual.publication_revision == supplied.publication_revision
                    && actual.host_epoch == supplied.host_epoch
                    && actual.handler_id == supplied.handler_id
                    && actual.expires_at == supplied.expires_at
            })
            .cloned()
    };

    let Some(entry) = entry else {
        auth_req
            .respond(Err(WireError::capability_denied(
                "invalid direct service grant",
            )))
            .await?;
        return Err(WireError::capability_denied("invalid direct service grant"));
    };

    if entry.value.expires_at <= unix_now()? {
        auth_req
            .respond(Err(WireError::capability_denied("direct grant expired")))
            .await?;
        return Err(WireError::capability_denied("direct grant expired"));
    }

    let _permit = entry
        .connections
        .clone()
        .try_acquire_owned()
        .map_err(|_| WireError::new("RATE_LIMITED", "direct grant connection limit reached"))?;
    let (_, handler) = services
        .get(&entry.value.handler_id)
        .ok_or_else(|| WireError::not_found("direct service handler missing"))?
        .clone();

    auth_req.respond(Ok(json!({ "ok": true }))).await?;

    let mut inflight = tokio::task::JoinSet::new();
    loop {
        tokio::select! {
            biased;
            _ = stop.cancelled() => break,
            _ = entry.cancelled.cancelled() => break,
            next = incoming.recv() => match next {
                Some(call) => {
                    if call.method != DIRECT_INVOKE {
                        let _ = call.respond(Err(WireError::invalid_request("expected direct.invoke"))).await;
                        continue;
                    }
                    if unix_now()? >= entry.value.expires_at {
                        let _ = call.respond(Err(WireError::capability_denied("direct grant expired"))).await;
                        break;
                    }
                    let Ok(parsed) = serde_json::from_value::<DirectInvoke>(Value::Object(call.params.clone())) else {
                        let _ = call.respond(Err(WireError::invalid_request("invalid direct.invoke payload"))).await;
                        continue;
                    };
                    let handler = handler.clone();
                    let rt = runtime.clone();
                    let grant_cancel = entry.cancelled.clone();
                    inflight.spawn(async move {
                        let call_cancel = call.cancelled.clone();
                        let combined = CancellationToken::new();
                        let c1 = combined.clone();
                        let watch = tokio::spawn(async move {
                            tokio::select! {
                                _ = call_cancel.cancelled() => c1.cancel(),
                                _ = grant_cancel.cancelled() => c1.cancel(),
                            }
                        });
                        let cfg = rt.config().unwrap_or(Value::Null);
                        let ctx = DirectContext {
                            config: cfg,
                            runtime: rt,
                            cancellation: combined,
                        };
                        let outcome = handler(parsed.input, ctx).await;
                        watch.abort();
                        let _ = call.respond(outcome).await;
                    });
                }
                None => break,
            },
            res = inflight.join_next(), if !inflight.is_empty() => {
                if let Some(Err(err)) = res {
                    tracing::warn!(%err, "direct invoke task failed");
                }
            }
        }
    }
    while let Some(res) = inflight.join_next().await {
        if let Err(err) = res {
            tracing::warn!(%err, "direct invoke shutdown error");
        }
    }
    owner.shutdown().await;
    Ok(())
}
