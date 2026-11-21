use astrbot_wire::{
    peer::Peer, CapabilityGrant, DirectGrant, DirectListen, InitializeParams, PluginManifest,
    METHOD_CONFIGURE, METHOD_INITIALIZE, METHOD_INVOKE_SERVICE, METHOD_INVOKE_TOOL, METHOD_PING,
    METHOD_SHUTDOWN, METHOD_TOOLS_DISCOVER, METHOD_VALIDATE_CONFIG, PROTOCOL_VERSION,
};
use astrbot_worker::{
    DirectContext, DiscoveredTool, HostClient, InvocationContext, MockHost, ServiceDefinition,
    StartupContext, ToolDefinition, Worker, WorkerError,
};
use serde_json::{json, Map, Value};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio_util::sync::CancellationToken;

fn test_manifest(id: &str, name: &str) -> PluginManifest {
    PluginManifest {
        schema_version: 2,
        id: id.to_owned(),
        name: name.to_owned(),
        description: "test".to_owned(),
        version: "0.1.0".to_owned(),
        author: "tester".to_owned(),
        language: "rust".to_owned(),
        entrypoint: None,
        command: vec!["bin/worker".to_owned()],
        sdk_version: None,
        host_version: None,
        target: None,
        capabilities: Default::default(),
        dependencies: BTreeMap::new(),
        optional_dependencies: BTreeMap::new(),
    }
}

#[tokio::test]
async fn mock_host_serves_storage_capabilities() {
    let mock = Arc::new(MockHost::new());
    let store = Arc::new(Mutex::new(BTreeMap::<String, Value>::new()));
    let s1 = store.clone();
    mock.on("storage.kv", "set", move |input| {
        let key = input["key"].as_str().unwrap().to_owned();
        s1.lock().unwrap().insert(key, input["value"].clone());
        Ok(json!({"ok": true}))
    });
    let s2 = store;
    mock.on("storage.kv", "get", move |input| {
        let key = input["key"].as_str().unwrap();
        let val = s2.lock().unwrap().get(key).cloned().unwrap_or(Value::Null);
        Ok(json!({"value": val}))
    });

    let client = HostClient::from_mock(mock);
    client.storage_set("k", json!(42)).await.unwrap();
    assert_eq!(
        client.storage_get("k", Value::Null).await.unwrap(),
        json!(42)
    );

    client.retire();
    let err = client.storage_get("k", Value::Null).await.unwrap_err();
    assert_eq!(err.code, "CALL_SCOPE_RETIRED");
}

#[tokio::test]
async fn live_config_validation_rollback_and_scoped_host_retirement() {
    let mut worker = Worker::from_manifest(test_manifest("tester/cfg", "cfg"));
    let saved_startup = Arc::new(Mutex::new(None::<StartupContext>));
    let saved_startup_ref = saved_startup.clone();
    let leaked_host = Arc::new(Mutex::new(None::<HostClient>));
    let leaked_host_ref = leaked_host.clone();

    worker
        .on_start(move |ctx| {
            let slot = saved_startup_ref.clone();
            async move {
                *slot.lock().unwrap() = Some(ctx);
                Ok(())
            }
        })
        .unwrap();

    worker
        .validate_config(|cfg| async move {
            if cfg.get("prefix").and_then(Value::as_str) == Some("bad") {
                return Err(WorkerError::invalid_config("prefix 'bad' is rejected"));
            }
            Ok(())
        })
        .unwrap();

    worker
        .tool(
            ToolDefinition::new("echo", "Echo with prefix", json!({"type": "object"})),
            move |args, ctx: InvocationContext| {
                let leak = leaked_host_ref.clone();
                async move {
                    *leak.lock().unwrap() = Some(ctx.host().clone());
                    let cfg = ctx.current_config()?;
                    let prefix = cfg.get("prefix").and_then(Value::as_str).unwrap_or("");
                    let text = args.get("text").and_then(Value::as_str).unwrap_or("");
                    Ok(json!({ "text": format!("{prefix}{text}") }))
                }
            },
        )
        .unwrap();

    let (host_io, worker_io) = tokio::io::duplex(16384);
    let (hr, hw) = tokio::io::split(host_io);
    let (wr, ww) = tokio::io::split(worker_io);

    let (host_owner, _host_in) = Peer::start(hr, hw, true);
    let worker_task = tokio::spawn(worker.run(wr, ww));
    let host_peer = host_owner.peer();
    let cancel = CancellationToken::new();

    let ping = host_peer
        .request(METHOD_PING, &json!({}), Duration::from_secs(2), &cancel)
        .await
        .unwrap();
    assert_eq!(ping["protocol_version"], json!(PROTOCOL_VERSION));

    let init = InitializeParams {
        protocol_versions: vec![PROTOCOL_VERSION],
        config: json!({"prefix": "v1:"}),
        host: Map::new(),
        capabilities: vec![CapabilityGrant {
            id: "storage.kv".into(),
            scope: Map::new(),
        }],
        features: vec![],
    };
    host_peer
        .request(METHOD_INITIALIZE, &init, Duration::from_secs(2), &cancel)
        .await
        .unwrap();

    // Candidate config validation rejects "bad" and keeps "v1:".
    let err = host_peer
        .request(
            METHOD_VALIDATE_CONFIG,
            &json!({"config": {"prefix": "bad"}}),
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "INVALID_CONFIG");

    // Live configure updates current_config() for both startup background ctx and new calls.
    host_peer
        .request(
            METHOD_CONFIGURE,
            &json!({"config": {"prefix": "v2:"}}),
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    let sctx = saved_startup.lock().unwrap().clone().unwrap();
    assert_eq!(sctx.current_config().unwrap(), json!({"prefix": "v2:"}));

    let res = host_peer
        .request(
            METHOD_INVOKE_TOOL,
            &json!({"handler_id": "tool:echo", "args": {"text": "hi"}}),
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    assert_eq!(res, json!({"text": "v2:hi"}));

    // Request-scoped HostClient captured during the tool call must already be retired!
    let leaked = leaked_host.lock().unwrap().clone().unwrap();
    let retired_err = leaked.storage_get("k", Value::Null).await.unwrap_err();
    assert_eq!(retired_err.code, "CALL_SCOPE_RETIRED");

    host_peer
        .request(
            METHOD_SHUTDOWN,
            &json!({"reason": "shutdown"}),
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    worker_task.await.unwrap().unwrap();
    host_owner.shutdown().await;
}

#[cfg(unix)]
#[tokio::test]
async fn direct_service_executes_without_host_and_enforces_revoke() {
    use std::os::unix::fs::PermissionsExt;

    let dir = tempfile::Builder::new()
        .prefix("ds-")
        .tempdir_in("/tmp")
        .unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let sock = dir.path().join("svc.sock");

    let mut worker = Worker::from_manifest(test_manifest("provider.svc", "svc"));
    worker
        .direct_service(
            ServiceDefinition::new("ping_svc", "Direct echo").direct(),
            |input, ctx: DirectContext| async move {
                Ok(json!({"echo": input, "cfg": ctx.current_config()?}))
            },
        )
        .unwrap();

    let (host_io, worker_io) = tokio::io::duplex(16384);
    let (hr, hw) = tokio::io::split(host_io);
    let (wr, ww) = tokio::io::split(worker_io);
    let (host_owner, _host_in) = Peer::start(hr, hw, true);
    let worker_task = tokio::spawn(worker.run(wr, ww));
    let host_peer = host_owner.peer();
    let cancel = CancellationToken::new();

    host_peer
        .request(
            METHOD_INITIALIZE,
            &InitializeParams {
                protocol_versions: vec![PROTOCOL_VERSION],
                config: json!({"mode": "fast"}),
                host: Map::new(),
                capabilities: vec![],
                features: vec![],
            },
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();

    host_peer
        .request(
            astrbot_wire::DIRECT_LISTEN,
            &DirectListen {
                socket_path: sock.to_string_lossy().into_owned(),
                provider_id: "provider.svc".into(),
                provider_generation: 1,
            },
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let grant = DirectGrant {
        id: "g1".into(),
        credential: "c".repeat(64),
        caller_id: "caller.svc".into(),
        caller_generation: 1,
        provider_id: "provider.svc".into(),
        provider_generation: 1,
        publication_revision: 1,
        host_epoch: "epoch-1".into(),
        handler_id: "service:ping_svc".into(),
        expires_at: now + 120,
    };
    let route_val = host_peer
        .request(
            astrbot_wire::DIRECT_GRANT,
            &grant,
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    let route: astrbot_wire::DirectRoute = serde_json::from_value(route_val).unwrap();
    assert_eq!(route.socket_path, sock.to_string_lossy());

    // Attach over UDS as caller and invoke direct.invoke.
    let stream = tokio::net::UnixStream::connect(&route.socket_path)
        .await
        .unwrap();
    let (r, w) = stream.into_split();
    let (caller_owner, _) = Peer::start(r, w, false);
    let caller = caller_owner.peer();

    caller
        .request(
            astrbot_wire::DIRECT_ATTACH,
            &grant,
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    let out = caller
        .request(
            astrbot_wire::DIRECT_INVOKE,
            &astrbot_wire::DirectInvoke {
                input: json!({"x": 7}),
            },
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    assert_eq!(out, json!({"echo": {"x": 7}, "cfg": {"mode": "fast"}}));

    host_peer
        .request(
            astrbot_wire::DIRECT_REVOKE,
            &astrbot_wire::RevokeGrant { id: "g1".into() },
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    let err = caller
        .request(
            astrbot_wire::DIRECT_INVOKE,
            &astrbot_wire::DirectInvoke {
                input: json!({"x": 8}),
            },
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap_err();
    assert_eq!(err.code, "HOST_UNAVAILABLE");

    caller_owner.shutdown().await;
    host_peer
        .request(METHOD_SHUTDOWN, &json!({}), Duration::from_secs(2), &cancel)
        .await
        .unwrap();
    worker_task.await.unwrap().unwrap();
    host_owner.shutdown().await;
}

#[tokio::test]
async fn runtime_tools_discover_and_invoke() {
    let mut worker = Worker::from_manifest(test_manifest("tester/dyn", "dyn"));
    let count = Arc::new(AtomicUsize::new(0));
    let count_ref = count.clone();
    worker
        .on_tool_discovery(move |_ctx| {
            let n = count_ref.fetch_add(1, Ordering::Relaxed) + 1;
            async move {
                Ok(vec![DiscoveredTool {
                    spec: astrbot_wire::DynamicToolSpec {
                        name: format!("dyn_tool_{n}"),
                        description: "dynamic tool".into(),
                        params: vec![],
                        parameters_schema: Some(json!({"type": "object"})),
                        resumable: false,
                        streaming_input: false,
                    },
                    handler: Arc::new(|args, _ctx| {
                        Box::pin(async move { Ok(json!({"dyn": args})) })
                    }),
                }])
            }
        })
        .unwrap();

    let (host_io, worker_io) = tokio::io::duplex(8192);
    let (hr, hw) = tokio::io::split(host_io);
    let (wr, ww) = tokio::io::split(worker_io);
    let (host_owner, _host_in) = Peer::start(hr, hw, true);
    let worker_task = tokio::spawn(worker.run(wr, ww));
    let peer = host_owner.peer();
    let cancel = CancellationToken::new();

    peer.request(
        METHOD_INITIALIZE,
        &InitializeParams {
            protocol_versions: vec![PROTOCOL_VERSION],
            config: json!({}),
            host: Map::new(),
            capabilities: vec![],
            features: vec![],
        },
        Duration::from_secs(2),
        &cancel,
    )
    .await
    .unwrap();

    let disc = peer
        .request(
            METHOD_TOOLS_DISCOVER,
            &json!({}),
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    assert_eq!(disc["version"], json!(1));
    assert_eq!(disc["tools"][0]["name"], json!("dyn_tool_1"));

    let out = peer
        .request(
            METHOD_INVOKE_TOOL,
            &json!({"handler_id": "tool:dyn_tool_1", "args": {"a": 1}}),
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap();
    assert_eq!(out, json!({"dyn": {"a": 1}}));

    let _ = peer
        .request(
            METHOD_INVOKE_SERVICE,
            &json!({"service": "missing"}),
            Duration::from_secs(2),
            &cancel,
        )
        .await
        .unwrap_err();
    peer.request(METHOD_SHUTDOWN, &json!({}), Duration::from_secs(2), &cancel)
        .await
        .unwrap();
    worker_task.await.unwrap().unwrap();
    host_owner.shutdown().await;
}
