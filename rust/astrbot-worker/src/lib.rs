//! Author SDK for AstrBot Rust and native Worker plugins.

pub mod context;
pub mod direct;
pub mod direct_client;
pub mod external;
pub mod host;
pub mod runtime_tools;
pub mod state;
pub mod tasks;

pub use astrbot_wire::{
    load_manifest, reject_external_schema_refs, validate_config_schema_file, CapabilityGrant,
    DirectListen, DynamicToolSpec, ExternalCredential, HandlerDescriptor, InitializeParams,
    InitializeResult, PluginManifest, PluginStopReason, ServiceDescriptor, ServiceMode,
    ToolDiscoveryResult, ToolParamSpec, WireError as WorkerError,
};
use context::IncomingStreamProxy;
pub use context::{DirectContext, InvocationContext, StartupContext};
pub use direct_client::PluginClient;
pub use external::load_external_credential;
pub use host::{HostBackend, HostClient, MockHost};
pub use runtime_tools::{DiscoveredTool, RuntimeTools};
pub use tasks::TaskGroup;

use astrbot_wire::{
    default_worker_features,
    peer::{Incoming, Peer, PeerOwner},
    DirectGrant, PluginSummary, RevokeGrant, WireError, DIRECT_GRANT, DIRECT_LISTEN, DIRECT_REVOKE,
    METHOD_CAPABILITIES_UPDATE, METHOD_CONFIGURE, METHOD_INITIALIZE, METHOD_INVOKE,
    METHOD_INVOKE_CRON, METHOD_INVOKE_HOOK, METHOD_INVOKE_SERVICE, METHOD_INVOKE_TOOL, METHOD_PING,
    METHOD_RESUMABLE_INVOKE, METHOD_SHUTDOWN, METHOD_TOOLS_DISCOVER, METHOD_VALIDATE_CONFIG,
    PROTOCOL_VERSION,
};
use direct::DirectServer;
use direct_client::DirectClients;
use futures::future::BoxFuture;
use serde_json::{json, Map, Value};
use state::RuntimeState;
use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio_util::sync::CancellationToken;

pub type InvocationHandler = Arc<
    dyn Fn(Value, InvocationContext) -> BoxFuture<'static, Result<Value, WorkerError>>
        + Send
        + Sync,
>;
pub type ServiceHandler = Arc<
    dyn Fn(Value, DirectContext) -> BoxFuture<'static, Result<Value, WorkerError>> + Send + Sync,
>;
pub type ConfigValidator =
    Arc<dyn Fn(Value) -> BoxFuture<'static, Result<(), WorkerError>> + Send + Sync>;
pub type ConfigChangeHandler =
    Arc<dyn Fn(Value, Value) -> BoxFuture<'static, Result<(), WorkerError>> + Send + Sync>;
pub type StartHandler =
    Arc<dyn Fn(StartupContext) -> BoxFuture<'static, Result<(), WorkerError>> + Send + Sync>;
pub type StopHandler =
    Arc<dyn Fn(PluginStopReason) -> BoxFuture<'static, Result<(), WorkerError>> + Send + Sync>;
pub type HealthHandler = Arc<dyn Fn() -> BoxFuture<'static, Result<(), WorkerError>> + Send + Sync>;
pub type DiscoveryHandler = Arc<
    dyn Fn(StartupContext) -> BoxFuture<'static, Result<Vec<DiscoveredTool>, WorkerError>>
        + Send
        + Sync,
>;

#[derive(Clone)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub params: Vec<ToolParamSpec>,
    pub parameters_schema: Option<Value>,
    pub resumable: bool,
    pub streaming_input: bool,
}

impl ToolDefinition {
    pub fn new(
        name: impl Into<String>,
        description: impl Into<String>,
        parameters_schema: Value,
    ) -> Self {
        Self {
            name: name.into(),
            description: description.into(),
            params: Vec::new(),
            parameters_schema: Some(parameters_schema),
            resumable: false,
            streaming_input: false,
        }
    }

    pub fn resumable(mut self) -> Self {
        self.resumable = true;
        self
    }

    pub fn streaming_input(mut self) -> Self {
        self.streaming_input = true;
        self
    }
}

#[derive(Clone)]
pub struct ServiceDefinition {
    pub name: String,
    pub description: String,
    pub mode: ServiceMode,
    pub input_schema: Option<Value>,
}

impl ServiceDefinition {
    pub fn new(name: impl Into<String>, description: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            description: description.into(),
            mode: ServiceMode::HostScoped,
            input_schema: None,
        }
    }

    pub fn direct(mut self) -> Self {
        self.mode = ServiceMode::Direct;
        self
    }
}

#[derive(Clone, Default)]
pub struct CommandDefinition {
    pub path: String,
    pub aliases: Vec<String>,
    pub description: Option<String>,
    pub priority: i64,
}

/// Export a Pydantic/Schemars JSON Schema to `<package_root>/config.schema.json`.
#[cfg(feature = "config-schema")]
pub fn export_config_schema<T: schemars::JsonSchema>(
    package_root: impl AsRef<Path>,
) -> Result<PathBuf, WorkerError> {
    let schema = schemars::schema_for!(T);
    let mut val = serde_json::to_value(&schema)
        .map_err(|err| WorkerError::invalid_config(err.to_string()))?;
    if let Some(obj) = val.as_object_mut() {
        obj.entry("type".to_owned())
            .or_insert_with(|| Value::String("object".to_owned()));
    }
    reject_external_schema_refs(&val)?;
    let mut content = serde_json::to_string_pretty(&val)
        .map_err(|err| WorkerError::invalid_config(err.to_string()))?;
    content.push('\n');
    if content.len() > astrbot_wire::MAX_CONFIG_SCHEMA_BYTES {
        return Err(WorkerError::invalid_config(
            "config.schema.json exceeds 64 KiB limit",
        ));
    }
    let out = package_root.as_ref().join("config.schema.json");
    std::fs::write(&out, content).map_err(|err| WorkerError::invalid_config(err.to_string()))?;
    Ok(out)
}

#[derive(Clone)]
struct RegisteredHandler {
    descriptor: HandlerDescriptor,
    handler: InvocationHandler,
}

#[derive(Clone)]
enum ServiceEntry {
    HostScoped(ServiceDescriptor, InvocationHandler),
    Direct(ServiceDescriptor, ServiceHandler),
}

/// Ergonomic async plugin Worker builder and protocol runtime.
#[derive(Clone)]
pub struct Worker {
    pub manifest: PluginManifest,
    pub plugin_root: Option<PathBuf>,
    config_validator_schema: Option<Value>,
    handlers: BTreeMap<String, RegisteredHandler>,
    tools: BTreeMap<String, (ToolDefinition, InvocationHandler)>,
    services: BTreeMap<String, ServiceEntry>,
    config_validator: Option<ConfigValidator>,
    config_changed: Option<ConfigChangeHandler>,
    on_start: Option<StartHandler>,
    on_stop: Option<StopHandler>,
    on_health: Option<HealthHandler>,
    tool_discovery: Option<DiscoveryHandler>,
    runtime_tools: RuntimeTools,
}

impl Worker {
    /// Construct a Worker from a plugin directory containing `plugin.json` or `metadata.yaml`.
    pub fn from_dir(plugin_root: impl AsRef<Path>) -> Result<Self, WorkerError> {
        let root = plugin_root.as_ref().to_path_buf();
        let manifest = load_manifest(&root)?;
        let schema_path = root.join("config.schema.json");
        let schema = if schema_path.is_file() {
            Some(validate_config_schema_file(&schema_path)?)
        } else {
            None
        };
        Ok(Self::from_manifest_with_root(manifest, Some(root), schema))
    }

    pub fn from_manifest(manifest: PluginManifest) -> Self {
        Self::from_manifest_with_root(manifest, None, None)
    }

    fn from_manifest_with_root(
        manifest: PluginManifest,
        plugin_root: Option<PathBuf>,
        config_validator_schema: Option<Value>,
    ) -> Self {
        Self {
            manifest,
            plugin_root,
            config_validator_schema,
            handlers: BTreeMap::new(),
            tools: BTreeMap::new(),
            services: BTreeMap::new(),
            config_validator: None,
            config_changed: None,
            on_start: None,
            on_stop: None,
            on_health: None,
            tool_discovery: None,
            runtime_tools: RuntimeTools::default(),
        }
    }

    pub fn runtime_tools(&self) -> RuntimeTools {
        self.runtime_tools.clone()
    }

    pub fn command<F, Fut>(
        &mut self,
        path: impl Into<String>,
        handler: F,
    ) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, InvocationContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, WorkerError>> + Send + 'static,
    {
        let path = path.into();
        self.command_with(
            CommandDefinition {
                path: path.clone(),
                aliases: Vec::new(),
                description: None,
                priority: 0,
            },
            handler,
        )
    }

    pub fn command_with<F, Fut>(
        &mut self,
        def: CommandDefinition,
        handler: F,
    ) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, InvocationContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, WorkerError>> + Send + 'static,
    {
        let id = format!("command:{}", def.path.replace(' ', "_"));
        if self.handlers.contains_key(&id) {
            return Err(WorkerError::conflict(format!("duplicate handler id: {id}")));
        }
        let mut details = Map::new();
        details.insert("path".into(), Value::String(def.path));
        details.insert(
            "aliases".into(),
            Value::Array(def.aliases.into_iter().map(Value::String).collect()),
        );
        details.insert("params".into(), Value::Array(Vec::new()));
        self.handlers.insert(
            id.clone(),
            RegisteredHandler {
                descriptor: HandlerDescriptor {
                    id,
                    kind: "command".to_owned(),
                    description: def.description,
                    priority: def.priority,
                    details,
                },
                handler: Arc::new(move |input, ctx| Box::pin(handler(input, ctx))),
            },
        );
        Ok(self)
    }

    pub fn tool<F, Fut>(
        &mut self,
        def: ToolDefinition,
        handler: F,
    ) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, InvocationContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, WorkerError>> + Send + 'static,
    {
        let handler_id = format!("tool:{}", def.name);
        if self.tools.contains_key(&handler_id) || self.handlers.contains_key(&handler_id) {
            return Err(WorkerError::conflict(format!(
                "duplicate tool: {}",
                def.name
            )));
        }
        let mut details = Map::new();
        details.insert("name".into(), Value::String(def.name.clone()));
        details.insert("description".into(), Value::String(def.description.clone()));
        details.insert("resumable".into(), Value::Bool(def.resumable));
        details.insert("streaming_input".into(), Value::Bool(def.streaming_input));
        if let Some(schema) = &def.parameters_schema {
            details.insert("parameters_schema".into(), schema.clone());
        }
        details.insert(
            "params".into(),
            serde_json::to_value(&def.params).unwrap_or_else(|_| Value::Array(Vec::new())),
        );
        let wrapped: InvocationHandler = Arc::new(move |input, ctx| Box::pin(handler(input, ctx)));
        self.handlers.insert(
            handler_id.clone(),
            RegisteredHandler {
                descriptor: HandlerDescriptor {
                    id: handler_id.clone(),
                    kind: "tool".to_owned(),
                    description: Some(def.description.clone()),
                    priority: 0,
                    details,
                },
                handler: wrapped.clone(),
            },
        );
        self.tools.insert(handler_id, (def, wrapped));
        Ok(self)
    }

    /// Register a Host-scoped cross-plugin service handler (receives `InvocationContext`).
    pub fn service<F, Fut>(
        &mut self,
        def: ServiceDefinition,
        handler: F,
    ) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, InvocationContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, WorkerError>> + Send + 'static,
    {
        if def.mode == ServiceMode::Direct {
            return Err(WorkerError::invalid_request(
                "use direct_service() for direct-mode services",
            ));
        }
        let handler_id = format!("service:{}", def.name);
        if self.services.contains_key(&handler_id) {
            return Err(WorkerError::conflict(format!(
                "duplicate service: {}",
                def.name
            )));
        }
        let desc = ServiceDescriptor {
            handler_id: handler_id.clone(),
            name: def.name,
            description: def.description,
            mode: ServiceMode::HostScoped,
            input_schema: def.input_schema,
        };
        self.services.insert(
            handler_id,
            ServiceEntry::HostScoped(
                desc,
                Arc::new(move |input, ctx| Box::pin(handler(input, ctx))),
            ),
        );
        Ok(self)
    }

    /// Register a direct-mode service handler receiving `DirectContext` (no HostClient).
    pub fn direct_service<F, Fut>(
        &mut self,
        def: ServiceDefinition,
        handler: F,
    ) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, DirectContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, WorkerError>> + Send + 'static,
    {
        let handler_id = format!("service:{}", def.name);
        if self.services.contains_key(&handler_id) {
            return Err(WorkerError::conflict(format!(
                "duplicate service: {}",
                def.name
            )));
        }
        let desc = ServiceDescriptor {
            handler_id: handler_id.clone(),
            name: def.name,
            description: def.description,
            mode: ServiceMode::Direct,
            input_schema: def.input_schema,
        };
        self.services.insert(
            handler_id,
            ServiceEntry::Direct(
                desc,
                Arc::new(move |input, ctx| Box::pin(handler(input, ctx))),
            ),
        );
        Ok(self)
    }

    pub fn web_route<F, Fut>(
        &mut self,
        path: impl Into<String>,
        methods: Vec<String>,
        description: impl Into<String>,
        handler: F,
    ) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, InvocationContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, WorkerError>> + Send + 'static,
    {
        let path = path.into();
        let id = format!("web:{path}");
        let mut details = Map::new();
        details.insert("route".into(), Value::String(path));
        details.insert(
            "methods".into(),
            Value::Array(methods.into_iter().map(Value::String).collect()),
        );
        self.handlers.insert(
            id.clone(),
            RegisteredHandler {
                descriptor: HandlerDescriptor {
                    id,
                    kind: "web_route".to_owned(),
                    description: Some(description.into()),
                    priority: 0,
                    details,
                },
                handler: Arc::new(move |input, ctx| Box::pin(handler(input, ctx))),
            },
        );
        Ok(self)
    }

    pub fn hook<F, Fut>(
        &mut self,
        stage: impl Into<String>,
        priority: i64,
        handler: F,
    ) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, InvocationContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, WorkerError>> + Send + 'static,
    {
        let stage = stage.into();
        let id = format!("hook:{stage}");
        let mut details = Map::new();
        details.insert("stage".into(), Value::String(stage.clone()));
        self.handlers.insert(
            id.clone(),
            RegisteredHandler {
                descriptor: HandlerDescriptor {
                    id,
                    kind: format!("hook.{stage}"),
                    description: None,
                    priority,
                    details,
                },
                handler: Arc::new(move |input, ctx| Box::pin(handler(input, ctx))),
            },
        );
        Ok(self)
    }

    pub fn validate_config<F, Fut>(&mut self, handler: F) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), WorkerError>> + Send + 'static,
    {
        self.config_validator = Some(Arc::new(move |cfg| Box::pin(handler(cfg))));
        Ok(self)
    }

    pub fn on_config_changed<F, Fut>(&mut self, handler: F) -> Result<&mut Self, WorkerError>
    where
        F: Fn(Value, Value) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), WorkerError>> + Send + 'static,
    {
        self.config_changed = Some(Arc::new(move |prev, curr| Box::pin(handler(prev, curr))));
        Ok(self)
    }

    pub fn on_start<F, Fut>(&mut self, handler: F) -> Result<&mut Self, WorkerError>
    where
        F: Fn(StartupContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), WorkerError>> + Send + 'static,
    {
        self.on_start = Some(Arc::new(move |ctx| Box::pin(handler(ctx))));
        Ok(self)
    }

    pub fn on_stop<F, Fut>(&mut self, handler: F) -> Result<&mut Self, WorkerError>
    where
        F: Fn(PluginStopReason) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), WorkerError>> + Send + 'static,
    {
        self.on_stop = Some(Arc::new(move |reason| Box::pin(handler(reason))));
        Ok(self)
    }

    pub fn on_health<F, Fut>(&mut self, handler: F) -> Result<&mut Self, WorkerError>
    where
        F: Fn() -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), WorkerError>> + Send + 'static,
    {
        self.on_health = Some(Arc::new(move || Box::pin(handler())));
        Ok(self)
    }

    pub fn on_tool_discovery<F, Fut>(&mut self, handler: F) -> Result<&mut Self, WorkerError>
    where
        F: Fn(StartupContext) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Vec<DiscoveredTool>, WorkerError>> + Send + 'static,
    {
        self.tool_discovery = Some(Arc::new(move |ctx| Box::pin(handler(ctx))));
        Ok(self)
    }

    /// Run over stdio, duplicating stdout fd for protocol frames so `println!` goes to stderr.
    pub async fn run_stdio(self) -> Result<(), WorkerError> {
        use std::io::Read;
        use tokio::io::AsyncWriteExt;

        #[cfg(unix)]
        let stdout = {
            use std::os::fd::FromRawFd;
            let raw_out = unsafe { libc_dup(1)? };
            unsafe { libc_dup2(2, 1)? };
            let out_file = unsafe { std::fs::File::from_raw_fd(raw_out) };
            tokio::fs::File::from_std(out_file)
        };
        #[cfg(not(unix))]
        let stdout = tokio::io::stdout();

        // Read stdin on a plain OS thread rather than tokio::io::stdin() (spawn_blocking),
        // so Tokio Runtime::drop never stalls waiting on a blocked OS read after shutdown.
        let (stdin_reader, mut stdin_feeder) = tokio::io::duplex(64 * 1024);
        let handle = tokio::runtime::Handle::current();
        std::thread::Builder::new()
            .name("astrbot-stdio-in".to_owned())
            .spawn(move || {
                let mut stdin = std::io::stdin().lock();
                let mut buf = [0u8; 16 * 1024];
                loop {
                    match stdin.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            if handle.block_on(stdin_feeder.write_all(&buf[..n])).is_err() {
                                break;
                            }
                        }
                    }
                }
            })
            .map_err(|err| WorkerError::runtime_error(err.to_string()))?;

        self.run(stdin_reader, stdout).await
    }

    /// Run over any async reader/writer pair.
    pub async fn run<R, W>(self, reader: R, writer: W) -> Result<(), WorkerError>
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let (owner, incoming) = Peer::start(reader, writer, false);
        self.run_peer(owner, incoming).await
    }

    /// Connect to a Host WebSocket listener using `Authorization: Bearer <token>`.
    pub async fn run_ws(self, url: &str, token: Option<&str>) -> Result<(), WorkerError> {
        external::run_ws(self, url, token).await
    }

    /// Connect to a Host Unix Domain Socket listener using an enrolled external credential.
    #[cfg(unix)]
    pub async fn run_uds(self, credential: ExternalCredential) -> Result<(), WorkerError> {
        external::run_uds(self, credential).await
    }

    pub(crate) async fn run_peer(
        self,
        owner: PeerOwner,
        mut incoming: tokio::sync::mpsc::Receiver<Incoming>,
    ) -> Result<(), WorkerError> {
        let peer = owner.peer();
        self.runtime_tools.bind_peer(peer.clone());

        let runtime = Arc::new(RuntimeState::new());
        let tasks = TaskGroup::default();
        let instance_cancel = CancellationToken::new();
        let direct_clients = Arc::new(DirectClients::default());
        let mut direct_server: Option<DirectServer> = None;
        let direct_services = Arc::new(
            self.services
                .iter()
                .filter_map(|(id, entry)| match entry {
                    ServiceEntry::Direct(desc, h) => Some((id.clone(), (desc.mode, h.clone()))),
                    ServiceEntry::HostScoped(_, _) => None,
                })
                .collect::<BTreeMap<_, _>>(),
        );

        let worker = Arc::new(self);
        let mut inflight = tokio::task::JoinSet::new();
        let mut stop_reason = PluginStopReason::ConnectionClosed;

        while let Some(call) = incoming.recv().await {
            if call.method == METHOD_PING {
                if let Some(health) = &worker.on_health {
                    match tokio::time::timeout(Duration::from_secs(2), health()).await {
                        Ok(Ok(())) => {}
                        Ok(Err(err)) => {
                            let _ = call.respond(Err(err)).await;
                            continue;
                        }
                        Err(_) => {
                            let _ = call
                                .respond(Err(WireError::new(
                                    "DEADLINE_EXCEEDED",
                                    "health check timed out",
                                )))
                                .await;
                            continue;
                        }
                    }
                }
                let _ = tasks.health();
                let _ = call
                    .respond(Ok(json!({ "protocol_version": PROTOCOL_VERSION })))
                    .await;
                continue;
            }

            if call.method == METHOD_INITIALIZE {
                if runtime.active.load(Ordering::Acquire) {
                    let _ = call
                        .respond(Err(WireError::invalid_request(
                            "Runner is already initialized",
                        )))
                        .await;
                    continue;
                }
                let params: InitializeParams =
                    match serde_json::from_value(Value::Object(call.params.clone())) {
                        Ok(p) => p,
                        Err(err) => {
                            let _ = call
                                .respond(Err(WireError::invalid_request(err.to_string())))
                                .await;
                            continue;
                        }
                    };
                if !params.protocol_versions.contains(&PROTOCOL_VERSION) {
                    let _ = call
                        .respond(Err(WireError::invalid_request(
                            "no supported protocol version",
                        )))
                        .await;
                    continue;
                }
                if let Err(err) = worker.validate_candidate_config(&params.config).await {
                    let _ = call.respond(Err(err)).await;
                    continue;
                }
                runtime.set_config(params.config.clone())?;
                runtime.set_capabilities(params.capabilities.clone())?;

                if let Some(start) = &worker.on_start {
                    let data_dir = worker
                        .plugin_root
                        .as_ref()
                        .map(|p| p.join("data").to_string_lossy().into_owned());
                    let sctx = StartupContext {
                        host: HostClient::for_instance(
                            peer.clone(),
                            runtime.clone(),
                            instance_cancel.clone(),
                        ),
                        runtime: runtime.clone(),
                        config: params.config,
                        data_dir,
                        tasks: tasks.clone(),
                        cancellation: instance_cancel.clone(),
                    };
                    if let Err(err) = start(sctx).await {
                        let _ = call.respond(Err(err)).await;
                        continue;
                    }
                }
                runtime.active.store(true, Ordering::Release);

                let negotiated: Vec<String> = {
                    let supported: BTreeSet<String> =
                        default_worker_features().into_iter().collect();
                    if params.features.is_empty() {
                        supported.into_iter().collect()
                    } else {
                        params
                            .features
                            .into_iter()
                            .filter(|f| supported.contains(f))
                            .collect()
                    }
                };

                let init_result = InitializeResult {
                    protocol_version: PROTOCOL_VERSION,
                    plugin: PluginSummary {
                        id: worker.manifest.id.clone(),
                        name: worker.manifest.name.clone(),
                        version: worker.manifest.version.clone(),
                        schema_version: worker.manifest.schema_version,
                    },
                    handlers: worker
                        .handlers
                        .values()
                        .map(|h| h.descriptor.clone())
                        .collect(),
                    web_routes: worker
                        .handlers
                        .values()
                        .filter(|h| h.descriptor.kind == "web_route")
                        .map(|h| {
                            json!({
                                "route": h.descriptor.details.get("route").cloned().unwrap_or(Value::Null),
                                "methods": h.descriptor.details.get("methods").cloned().unwrap_or(json!(["GET"])),
                                "description": h.descriptor.description.clone().unwrap_or_default(),
                            })
                        })
                        .collect(),
                    services: worker
                        .services
                        .values()
                        .map(|entry| match entry {
                            ServiceEntry::HostScoped(d, _) | ServiceEntry::Direct(d, _) => d.clone(),
                        })
                        .collect(),
                    capabilities: params.capabilities.into_iter().map(|g| g.id).collect(),
                    features: negotiated,
                };
                let _ = call
                    .respond(Ok(serde_json::to_value(init_result).unwrap_or(Value::Null)))
                    .await;
                continue;
            }

            if call.method == METHOD_VALIDATE_CONFIG {
                let candidate = call.params.get("config").cloned().unwrap_or(Value::Null);
                let res = worker
                    .validate_candidate_config(&candidate)
                    .await
                    .map(|_| json!({ "valid": true }));
                let _ = call.respond(res).await;
                continue;
            }

            if call.method == METHOD_CONFIGURE {
                let candidate = call.params.get("config").cloned().unwrap_or(Value::Null);
                if let Err(err) = worker.validate_candidate_config(&candidate).await {
                    let _ = call.respond(Err(err)).await;
                    continue;
                }
                let prev = runtime.config().unwrap_or(Value::Null);
                runtime.set_config(candidate.clone())?;
                if let Some(cb) = &worker.config_changed {
                    if let Err(err) = cb(prev.clone(), candidate).await {
                        let _ = runtime.set_config(prev);
                        let _ = call.respond(Err(err)).await;
                        continue;
                    }
                }
                let _ = call.respond(Ok(json!({ "configured": true }))).await;
                continue;
            }

            if call.method == METHOD_CAPABILITIES_UPDATE {
                let grants: Vec<CapabilityGrant> = match serde_json::from_value(
                    call.params
                        .get("capabilities")
                        .cloned()
                        .unwrap_or_else(|| Value::Array(Vec::new())),
                ) {
                    Ok(g) => g,
                    Err(err) => {
                        let _ = call
                            .respond(Err(WireError::invalid_request(err.to_string())))
                            .await;
                        continue;
                    }
                };
                runtime.set_capabilities(grants.clone())?;
                let _ = call
                    .respond(Ok(json!({
                        "capabilities": grants.into_iter().map(|g| g.id).collect::<Vec<_>>()
                    })))
                    .await;
                continue;
            }

            if call.method == DIRECT_LISTEN {
                let opts: DirectListen =
                    match serde_json::from_value(Value::Object(call.params.clone())) {
                        Ok(o) => o,
                        Err(err) => {
                            let _ = call
                                .respond(Err(WireError::invalid_request(err.to_string())))
                                .await;
                            continue;
                        }
                    };
                if let Some(mut old) = direct_server.take() {
                    old.shutdown().await;
                }
                match DirectServer::bind(
                    opts,
                    &worker.manifest.id,
                    direct_services.clone(),
                    runtime.clone(),
                ) {
                    Ok(srv) => {
                        direct_server = Some(srv);
                        let _ = call.respond(Ok(json!({ "listening": true }))).await;
                    }
                    Err(err) => {
                        let _ = call.respond(Err(err)).await;
                    }
                }
                continue;
            }

            if call.method == DIRECT_GRANT {
                let grant: DirectGrant =
                    match serde_json::from_value(Value::Object(call.params.clone())) {
                        Ok(g) => g,
                        Err(err) => {
                            let _ = call
                                .respond(Err(WireError::invalid_request(err.to_string())))
                                .await;
                            continue;
                        }
                    };
                match &direct_server {
                    Some(srv) => {
                        let res = srv.install(grant, &direct_services).await.and_then(|r| {
                            serde_json::to_value(r)
                                .map_err(|e| WireError::runtime_error(e.to_string()))
                        });
                        let _ = call.respond(res).await;
                    }
                    None => {
                        let _ = call
                            .respond(Err(WireError::invalid_request(
                                "direct server is not listening",
                            )))
                            .await;
                    }
                }
                continue;
            }

            if call.method == DIRECT_REVOKE {
                let req: RevokeGrant =
                    match serde_json::from_value(Value::Object(call.params.clone())) {
                        Ok(r) => r,
                        Err(err) => {
                            let _ = call
                                .respond(Err(WireError::invalid_request(err.to_string())))
                                .await;
                            continue;
                        }
                    };
                if let Some(srv) = &direct_server {
                    let _ = srv.revoke(&req.id).await;
                }
                let _ = call.respond(Ok(json!({ "revoked": true }))).await;
                continue;
            }

            if call.method == METHOD_TOOLS_DISCOVER {
                if let Some(discover) = &worker.tool_discovery {
                    let sctx = StartupContext {
                        host: HostClient::for_instance(
                            peer.clone(),
                            runtime.clone(),
                            instance_cancel.clone(),
                        ),
                        runtime: runtime.clone(),
                        config: runtime.config().unwrap_or(Value::Null),
                        data_dir: None,
                        tasks: tasks.clone(),
                        cancellation: instance_cancel.clone(),
                    };
                    match discover(sctx).await {
                        Ok(tools) => {
                            if let Err(err) = worker.runtime_tools.replace_local(tools) {
                                let _ = call.respond(Err(err)).await;
                                continue;
                            }
                        }
                        Err(err) => {
                            let _ = call.respond(Err(err)).await;
                            continue;
                        }
                    }
                }
                let snap = worker.runtime_tools.snapshot()?;
                let _ = call
                    .respond(Ok(serde_json::to_value(snap).unwrap_or(Value::Null)))
                    .await;
                continue;
            }

            if call.method == METHOD_SHUTDOWN {
                if let Some(reason_val) = call.params.get("reason") {
                    if let Ok(parsed) =
                        serde_json::from_value::<PluginStopReason>(reason_val.clone())
                    {
                        stop_reason = parsed;
                    } else {
                        stop_reason = PluginStopReason::Shutdown;
                    }
                } else {
                    stop_reason = PluginStopReason::Shutdown;
                }
                let _ = call.respond(Ok(Value::Null)).await;
                break;
            }

            // Spawn handler invocation concurrently on JoinSet so control plane never blocks.
            let w = worker.clone();
            let rt = runtime.clone();
            let p = peer.clone();
            let dc = direct_clients.clone();
            inflight.spawn(async move {
                dispatch_invocation(w, rt, p, dc, call).await;
            });
        }

        runtime.mark_shutting_down();
        instance_cancel.cancel();
        if let Some(mut srv) = direct_server.take() {
            srv.shutdown().await;
        }
        direct_clients.close().await;
        while inflight.join_next().await.is_some() {}
        let _ = tasks.shutdown().await;
        if let Some(stop) = &worker.on_stop {
            let _ = tokio::time::timeout(Duration::from_secs(5), stop(stop_reason)).await;
        }
        owner.shutdown().await;
        Ok(())
    }

    async fn validate_candidate_config(&self, candidate: &Value) -> Result<(), WorkerError> {
        if let Some(schema) = &self.config_validator_schema {
            let validator = jsonschema::validator_for(schema)
                .map_err(|err| WorkerError::invalid_config(err.to_string()))?;
            let normalized = if candidate.is_null() {
                json!({})
            } else {
                candidate.clone()
            };
            if let Err(err) = validator.validate(&normalized) {
                return Err(WorkerError::invalid_config(err.to_string()));
            }
        }
        if let Some(validator) = &self.config_validator {
            validator(candidate.clone()).await?;
        }
        Ok(())
    }
}

async fn dispatch_invocation(
    worker: Arc<Worker>,
    runtime: Arc<RuntimeState>,
    peer: Peer,
    direct_clients: Arc<DirectClients>,
    call: Incoming,
) {
    let permit = match runtime.capacity.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => {
            let _ = call
                .respond(Err(WireError::new(
                    "RATE_LIMITED",
                    "worker concurrency limit reached",
                )))
                .await;
            return;
        }
    };

    let retired = Arc::new(AtomicBool::new(false));
    struct RetireOnDrop(Arc<AtomicBool>);
    impl Drop for RetireOnDrop {
        fn drop(&mut self) {
            self.0.store(true, Ordering::Release);
        }
    }
    let _retire_guard = RetireOnDrop(retired.clone());

    let call_id = call.id.as_str().to_owned();
    let cfg = runtime.config().unwrap_or(Value::Null);

    let (stream_tx, mut stream_rx) = tokio::sync::mpsc::channel::<(
        u64,
        Value,
        bool,
        tokio::sync::oneshot::Sender<Result<(), WireError>>,
    )>(16);
    let stream_proxy = Arc::new(IncomingStreamProxy::new(stream_tx));

    let ctx = InvocationContext {
        call_id: call_id.clone(),
        host: HostClient::for_invocation(
            peer.clone(),
            call_id.clone(),
            runtime.clone(),
            retired.clone(),
            call.cancelled.clone(),
        ),
        plugins: PluginClient::for_invocation(
            peer,
            call_id,
            direct_clients,
            retired.clone(),
            call.cancelled.clone(),
        ),
        runtime,
        incoming: Some(stream_proxy),
        config: cfg,
        cancellation: call.cancelled.clone(),
    };

    let method = call.method.clone();
    let params = call.params.clone();
    let is_yield_invocation = matches!(method.as_str(), METHOD_INVOKE | METHOD_RESUMABLE_INVOKE);

    let exec_fut: BoxFuture<'static, Result<Value, WorkerError>> = match method.as_str() {
        METHOD_INVOKE | METHOD_RESUMABLE_INVOKE => {
            let handler_id = params
                .get("handler_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned();
            if let Some(entry) = worker.handlers.get(&handler_id) {
                let input = Value::Object(params);
                (entry.handler)(input, ctx)
            } else if let Some(dyn_tool) = worker
                .runtime_tools
                .get(handler_id.strip_prefix("tool:").unwrap_or(&handler_id))
            {
                let args = params.get("args").cloned().unwrap_or(Value::Object(params));
                dyn_tool(args, ctx)
            } else {
                Box::pin(async move {
                    Err(WireError::not_found(format!(
                        "unknown handler: {handler_id}"
                    )))
                })
            }
        }
        METHOD_INVOKE_TOOL => {
            let handler_id = params
                .get("handler_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned();
            let args = params.get("args").cloned().unwrap_or_else(|| json!({}));
            if let Some((_, handler)) = worker.tools.get(&handler_id) {
                handler(args, ctx)
            } else if let Some(dyn_tool) = worker
                .runtime_tools
                .get(handler_id.strip_prefix("tool:").unwrap_or(&handler_id))
            {
                dyn_tool(args, ctx)
            } else {
                Box::pin(
                    async move { Err(WireError::not_found(format!("unknown tool: {handler_id}"))) },
                )
            }
        }
        METHOD_INVOKE_SERVICE => {
            let service = params
                .get("service")
                .and_then(Value::as_str)
                .map(|s| format!("service:{s}"))
                .or_else(|| {
                    params
                        .get("handler_id")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
                .unwrap_or_default();
            let input = params.get("input").cloned().unwrap_or_else(|| json!({}));
            match worker.services.get(&service) {
                Some(ServiceEntry::HostScoped(_, handler)) => handler(input, ctx),
                Some(ServiceEntry::Direct(_, handler)) => {
                    let dctx = DirectContext {
                        config: ctx.config,
                        runtime: ctx.runtime,
                        cancellation: ctx.cancellation,
                    };
                    handler(input, dctx)
                }
                None => Box::pin(async move {
                    Err(WireError::not_found(format!("unknown service: {service}")))
                }),
            }
        }
        METHOD_INVOKE_HOOK | METHOD_INVOKE_CRON => {
            let handler_id = params
                .get("handler_id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned();
            if let Some(entry) = worker.handlers.get(&handler_id) {
                (entry.handler)(Value::Object(params), ctx)
            } else {
                Box::pin(async move {
                    Err(WireError::not_found(format!(
                        "unknown handler: {handler_id}"
                    )))
                })
            }
        }
        _ => Box::pin(async move {
            Err(WireError::invalid_request(format!(
                "unknown method: {method}"
            )))
        }),
    };

    let mut seq_counter: u64 = 0;

    tokio::pin!(exec_fut);
    let outcome = loop {
        tokio::select! {
            res = &mut exec_fut => break res,
            Some((seq, val, wait_ack, ack_tx)) = stream_rx.recv() => {
                seq_counter = seq;
                let res = call.send_yield(seq, val, wait_ack).await;
                let _ = ack_tx.send(res);
            }
        }
    };
    retired.store(true, Ordering::Release);
    drop(permit);

    match outcome {
        Ok(value) if is_yield_invocation && !value.is_null() => {
            let stop = value
                .get("propagation")
                .and_then(Value::as_str)
                .is_some_and(|p| p == "stop");
            let next_seq = seq_counter + 1;
            if let Err(err) = call.send_yield(next_seq, value, !stop).await {
                let _ = call.respond(Err(err)).await;
                return;
            }
            let _ = call.respond(Ok(Value::Null)).await;
        }
        other => {
            let _ = call.respond(other).await;
        }
    }
}

#[cfg(unix)]
unsafe fn libc_dup(fd: i32) -> Result<i32, WorkerError> {
    extern "C" {
        fn dup(fildes: i32) -> i32;
    }
    let out = dup(fd);
    if out < 0 {
        return Err(WorkerError::runtime_error("failed to dup stdout fd"));
    }
    Ok(out)
}

#[cfg(unix)]
unsafe fn libc_dup2(fd: i32, fd2: i32) -> Result<(), WorkerError> {
    extern "C" {
        fn dup2(fildes: i32, fildes2: i32) -> i32;
    }
    if dup2(fd, fd2) < 0 {
        return Err(WorkerError::runtime_error(
            "failed to redirect stdout to stderr",
        ));
    }
    Ok(())
}
