//! PyO3 native extension binding `astrbot-worker` and `astrbot-wire` for Python plugins.

use astrbot_wire::{
    has_sensitive_schema_fields, load_manifest, validate_config_schema_file, PluginStopReason,
    WireError,
};
use astrbot_worker::{
    load_external_credential, CommandDefinition, DirectContext, DiscoveredTool, HostClient,
    InvocationContext, PluginClient, ServiceDefinition, StartupContext, ToolDefinition, Worker,
};
use futures::future::BoxFuture;
use pyo3::{exceptions::PyRuntimeError, prelude::*, PyTraverseError, PyVisit};
use pyo3_async_runtimes::{tokio::future_into_py, TaskLocals};
use serde_json::Value;
use std::sync::{Arc, Mutex};

fn py_err(err: WireError) -> PyErr {
    let data = err
        .data
        .as_ref()
        .map(|v| v.to_string())
        .unwrap_or_else(|| "null".to_owned());
    PyRuntimeError::new_err((err.code, err.message, data))
}

fn decode_json(raw: &str) -> PyResult<Value> {
    serde_json::from_str(raw).map_err(|err| PyRuntimeError::new_err(err.to_string()))
}

fn encode_json(value: &Value) -> PyResult<String> {
    serde_json::to_string(value).map_err(|err| PyRuntimeError::new_err(err.to_string()))
}

fn lock<T>(m: &Mutex<T>) -> PyResult<std::sync::MutexGuard<'_, T>> {
    m.lock()
        .map_err(|_| PyRuntimeError::new_err("native worker lock poisoned"))
}

fn to_wire_error(err: PyErr) -> WireError {
    Python::attach(|py| {
        let obj = err.value(py);
        if let (Ok(code), Ok(message)) = (
            obj.getattr("code").and_then(|v| v.extract::<String>()),
            obj.getattr("message").and_then(|v| v.extract::<String>()),
        ) {
            WireError::new(code, message)
        } else if let Ok(code) = obj.getattr("code").and_then(|v| v.extract::<String>()) {
            WireError::new(code, obj.to_string())
        } else {
            err.print(py);
            WireError::runtime_error("Python callback failed; see stderr")
        }
    })
}

#[pyclass(name = "NativeHostClient")]
#[derive(Clone)]
struct PyHostClient {
    inner: HostClient,
}

#[pymethods]
impl PyHostClient {
    fn invoke<'py>(
        &self,
        py: Python<'py>,
        capability: String,
        operation: String,
        input_json: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let input = decode_json(&input_json)?;
        let client = self.inner.clone();
        future_into_py(py, async move {
            let out = client
                .invoke(&capability, &operation, input)
                .await
                .map_err(py_err)?;
            encode_json(&out)
        })
    }

    fn is_retired(&self) -> bool {
        self.inner.is_retired()
    }

    fn retire(&self) {
        self.inner.retire();
    }
}

#[pyclass(name = "NativePluginClient")]
#[derive(Clone)]
struct PyPluginClient {
    inner: PluginClient,
}

#[pymethods]
impl PyPluginClient {
    fn call_service<'py>(
        &self,
        py: Python<'py>,
        target_plugin_id: String,
        service: String,
        input_json: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let input = decode_json(&input_json)?;
        let client = self.inner.clone();
        future_into_py(py, async move {
            let out = client
                .call_service(&target_plugin_id, &service, input)
                .await
                .map_err(py_err)?;
            encode_json(&out)
        })
    }
}

#[pyclass(name = "NativeStartupContext")]
#[derive(Clone)]
struct PyStartupContext {
    inner: StartupContext,
    config_json: String,
}

#[pymethods]
impl PyStartupContext {
    #[getter]
    fn config_json(&self) -> &str {
        &self.config_json
    }

    #[getter]
    fn data_dir(&self) -> Option<&str> {
        self.inner.data_dir.as_deref()
    }

    #[getter]
    fn host(&self) -> PyHostClient {
        PyHostClient {
            inner: self.inner.host().clone(),
        }
    }

    fn current_config_json(&self) -> PyResult<String> {
        let cfg = self.inner.current_config().map_err(py_err)?;
        encode_json(&cfg)
    }

    fn is_cancelled(&self) -> bool {
        self.inner.cancellation.is_cancelled()
    }
}

#[pyclass(name = "NativeInvocationContext")]
#[derive(Clone)]
struct PyInvocationContext {
    inner: InvocationContext,
    config_json: String,
}

#[pymethods]
impl PyInvocationContext {
    #[getter]
    fn call_id(&self) -> &str {
        &self.inner.call_id
    }

    #[getter]
    fn config_json(&self) -> &str {
        &self.config_json
    }

    #[getter]
    fn host(&self) -> PyHostClient {
        PyHostClient {
            inner: self.inner.host().clone(),
        }
    }

    #[getter]
    fn plugins(&self) -> PyPluginClient {
        PyPluginClient {
            inner: self.inner.plugins().clone(),
        }
    }

    fn current_config_json(&self) -> PyResult<String> {
        let cfg = self.inner.current_config().map_err(py_err)?;
        encode_json(&cfg)
    }

    #[pyo3(signature = (value_json, wait_for_ack = true))]
    fn yield_value<'py>(
        &self,
        py: Python<'py>,
        value_json: String,
        wait_for_ack: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let value = decode_json(&value_json)?;
        let ctx = self.inner.clone();
        future_into_py(py, async move {
            ctx.yield_value(value, wait_for_ack).await.map_err(py_err)
        })
    }

    fn progress<'py>(&self, py: Python<'py>, payload_json: String) -> PyResult<Bound<'py, PyAny>> {
        let value = decode_json(&payload_json)?;
        let ctx = self.inner.clone();
        future_into_py(py, async move { ctx.progress(value).await.map_err(py_err) })
    }

    fn is_cancelled(&self) -> bool {
        self.inner.cancellation.is_cancelled()
    }
}

#[pyclass(name = "NativeDirectContext")]
#[derive(Clone)]
struct PyDirectContext {
    inner: DirectContext,
    config_json: String,
}

#[pymethods]
impl PyDirectContext {
    #[getter]
    fn config_json(&self) -> &str {
        &self.config_json
    }

    fn current_config_json(&self) -> PyResult<String> {
        let cfg = self.inner.current_config().map_err(py_err)?;
        encode_json(&cfg)
    }

    fn is_cancelled(&self) -> bool {
        self.inner.cancellation.is_cancelled()
    }
}

#[derive(Clone)]
struct AsyncCallback {
    func: Arc<Py<PyAny>>,
    locals: Arc<TaskLocals>,
}

impl AsyncCallback {
    async fn call_two(&self, arg1: String, arg2: Py<PyAny>) -> Result<Value, WireError> {
        let future = Python::attach(|py| {
            let coro = self.func.bind(py).call1((arg1, arg2))?;
            pyo3_async_runtimes::into_future_with_locals(&self.locals, coro)
        })
        .map_err(to_wire_error)?;
        let res = future.await.map_err(to_wire_error)?;
        Python::attach(|py| {
            let bound = res.bind(py);
            if bound.is_none() {
                return Ok(Value::Null);
            }
            let json_str: String = bound.extract()?;
            serde_json::from_str(&json_str).map_err(|err| PyRuntimeError::new_err(err.to_string()))
        })
        .map_err(to_wire_error)
    }

    async fn call_one_json(&self, arg1: String) -> Result<Value, WireError> {
        let future = Python::attach(|py| {
            let coro = self.func.bind(py).call1((arg1,))?;
            pyo3_async_runtimes::into_future_with_locals(&self.locals, coro)
        })
        .map_err(to_wire_error)?;
        let res = future.await.map_err(to_wire_error)?;
        Python::attach(|py| {
            let bound = res.bind(py);
            if bound.is_none() {
                return Ok(Value::Null);
            }
            let json_str: String = bound.extract()?;
            serde_json::from_str(&json_str).map_err(|err| PyRuntimeError::new_err(err.to_string()))
        })
        .map_err(to_wire_error)
    }

    async fn call_zero(&self) -> Result<(), WireError> {
        let future = Python::attach(|py| {
            let coro = self.func.bind(py).call0()?;
            pyo3_async_runtimes::into_future_with_locals(&self.locals, coro)
        })
        .map_err(to_wire_error)?;
        future.await.map_err(to_wire_error)?;
        Ok(())
    }
}

enum RegOp {
    Command {
        path: String,
        aliases: Vec<String>,
        description: Option<String>,
        priority: i64,
        cb: Arc<Py<PyAny>>,
    },
    Tool {
        name: String,
        description: String,
        schema: Value,
        resumable: bool,
        streaming_input: bool,
        cb: Arc<Py<PyAny>>,
    },
    Service {
        name: String,
        description: String,
        direct: bool,
        schema: Option<Value>,
        cb: Arc<Py<PyAny>>,
    },
    Hook {
        stage: String,
        priority: i64,
        cb: Arc<Py<PyAny>>,
    },
    WebRoute {
        route: String,
        methods: Vec<String>,
        description: String,
        cb: Arc<Py<PyAny>>,
    },
    ValidateConfig {
        cb: Arc<Py<PyAny>>,
    },
    ConfigChanged {
        cb: Arc<Py<PyAny>>,
    },
    Start {
        cb: Arc<Py<PyAny>>,
    },
    Stop {
        cb: Arc<Py<PyAny>>,
    },
    Health {
        cb: Arc<Py<PyAny>>,
    },
    ToolDiscovery {
        cb: Arc<Py<PyAny>>,
    },
}

struct WorkerBuild {
    worker: Worker,
    ops: Vec<RegOp>,
}

#[pyclass(name = "NativeWorker")]
struct PyNativeWorker {
    inner: Mutex<Option<WorkerBuild>>,
}

#[pymethods]
impl PyNativeWorker {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(guard) = self.inner.try_lock() {
            if let Some(build) = guard.as_ref() {
                for op in &build.ops {
                    let cb = match op {
                        RegOp::Command { cb, .. }
                        | RegOp::Tool { cb, .. }
                        | RegOp::Service { cb, .. }
                        | RegOp::Hook { cb, .. }
                        | RegOp::WebRoute { cb, .. }
                        | RegOp::ValidateConfig { cb }
                        | RegOp::ConfigChanged { cb }
                        | RegOp::Start { cb }
                        | RegOp::Stop { cb }
                        | RegOp::Health { cb }
                        | RegOp::ToolDiscovery { cb } => cb,
                    };
                    visit.call(cb.as_ref())?;
                }
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        if let Ok(mut guard) = self.inner.lock() {
            guard.take();
        }
    }

    #[staticmethod]
    fn from_dir(plugin_root: String) -> PyResult<Self> {
        let worker = Worker::from_dir(&plugin_root).map_err(py_err)?;
        Ok(Self {
            inner: Mutex::new(Some(WorkerBuild {
                worker,
                ops: Vec::new(),
            })),
        })
    }

    #[pyo3(signature = (path, aliases, description, priority, callback))]
    fn register_command(
        &self,
        path: String,
        aliases: Vec<String>,
        description: Option<String>,
        priority: i64,
        callback: Py<PyAny>,
    ) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::Command {
            path,
            aliases,
            description,
            priority,
            cb: Arc::new(callback),
        });
        Ok(())
    }

    #[pyo3(signature = (name, description, parameters_schema_json, resumable, streaming_input, callback))]
    fn register_tool(
        &self,
        name: String,
        description: String,
        parameters_schema_json: String,
        resumable: bool,
        streaming_input: bool,
        callback: Py<PyAny>,
    ) -> PyResult<()> {
        let schema = decode_json(&parameters_schema_json)?;
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::Tool {
            name,
            description,
            schema,
            resumable,
            streaming_input,
            cb: Arc::new(callback),
        });
        Ok(())
    }

    #[pyo3(signature = (name, description, direct, input_schema_json, callback))]
    fn register_service(
        &self,
        name: String,
        description: String,
        direct: bool,
        input_schema_json: Option<String>,
        callback: Py<PyAny>,
    ) -> PyResult<()> {
        let schema = input_schema_json.as_deref().map(decode_json).transpose()?;
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::Service {
            name,
            description,
            direct,
            schema,
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_hook(&self, stage: String, priority: i64, callback: Py<PyAny>) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::Hook {
            stage,
            priority,
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_web_route(
        &self,
        route: String,
        methods: Vec<String>,
        description: String,
        callback: Py<PyAny>,
    ) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::WebRoute {
            route,
            methods,
            description,
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_validate_config(&self, callback: Py<PyAny>) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::ValidateConfig {
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_on_config_changed(&self, callback: Py<PyAny>) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::ConfigChanged {
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_on_start(&self, callback: Py<PyAny>) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::Start {
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_on_stop(&self, callback: Py<PyAny>) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::Stop {
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_on_health(&self, callback: Py<PyAny>) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::Health {
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn register_tool_discovery(&self, callback: Py<PyAny>) -> PyResult<()> {
        let mut guard = lock(&self.inner)?;
        let build = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("worker already started"))?;
        build.ops.push(RegOp::ToolDiscovery {
            cb: Arc::new(callback),
        });
        Ok(())
    }

    fn run_stdio<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let worker = self.finalize_worker(py)?;
        future_into_py(py, async move { worker.run_stdio().await.map_err(py_err) })
    }

    #[pyo3(signature = (url, token = None))]
    fn run_ws<'py>(
        &self,
        py: Python<'py>,
        url: String,
        token: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let worker = self.finalize_worker(py)?;
        future_into_py(py, async move {
            worker.run_ws(&url, token.as_deref()).await.map_err(py_err)
        })
    }

    #[cfg(unix)]
    fn run_uds<'py>(
        &self,
        py: Python<'py>,
        credential_path: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let cred = load_external_credential(&credential_path).map_err(py_err)?;
        let worker = self.finalize_worker(py)?;
        future_into_py(
            py,
            async move { worker.run_uds(cred).await.map_err(py_err) },
        )
    }
}

impl PyNativeWorker {
    fn finalize_worker(&self, py: Python<'_>) -> PyResult<Worker> {
        let build = lock(&self.inner)?
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("worker can only run once"))?;
        let locals = Arc::new(pyo3_async_runtimes::tokio::get_current_locals(py)?);
        let mut worker = build.worker;
        for op in build.ops {
            match op {
                RegOp::Command {
                    path,
                    aliases,
                    description,
                    priority,
                    cb,
                } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .command_with(
                            CommandDefinition {
                                path,
                                aliases,
                                description,
                                priority,
                            },
                            move |input, ctx| {
                                let callback = callback.clone();
                                invoke_py_with_ctx(callback, input, ctx)
                            },
                        )
                        .map_err(py_err)?;
                }
                RegOp::Tool {
                    name,
                    description,
                    schema,
                    resumable,
                    streaming_input,
                    cb,
                } => {
                    let mut def = ToolDefinition::new(name, description, schema);
                    if resumable {
                        def = def.resumable();
                    }
                    if streaming_input {
                        def = def.streaming_input();
                    }
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .tool(def, move |input, ctx| {
                            let callback = callback.clone();
                            invoke_py_with_ctx(callback, input, ctx)
                        })
                        .map_err(py_err)?;
                }
                RegOp::Service {
                    name,
                    description,
                    direct,
                    schema,
                    cb,
                } => {
                    let mut def = ServiceDefinition::new(name, description);
                    def.input_schema = schema;
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    if direct {
                        def = def.direct();
                        worker
                            .direct_service(def, move |input, ctx: DirectContext| {
                                let callback = callback.clone();
                                async move {
                                    let input_json = serde_json::to_string(&input)
                                        .map_err(|e| WireError::invalid_request(e.to_string()))?;
                                    let cfg_json = serde_json::to_string(&ctx.config)
                                        .map_err(|e| WireError::runtime_error(e.to_string()))?;
                                    let py_ctx = Python::attach(|py| {
                                        Py::new(
                                            py,
                                            PyDirectContext {
                                                inner: ctx,
                                                config_json: cfg_json,
                                            },
                                        )
                                    })
                                    .map_err(to_wire_error)?;
                                    callback.call_two(input_json, py_ctx.into_any()).await
                                }
                            })
                            .map_err(py_err)?;
                    } else {
                        worker
                            .service(def, move |input, ctx| {
                                let callback = callback.clone();
                                invoke_py_with_ctx(callback, input, ctx)
                            })
                            .map_err(py_err)?;
                    }
                }
                RegOp::Hook {
                    stage,
                    priority,
                    cb,
                } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .hook(stage, priority, move |input, ctx| {
                            let callback = callback.clone();
                            invoke_py_with_ctx(callback, input, ctx)
                        })
                        .map_err(py_err)?;
                }
                RegOp::WebRoute {
                    route,
                    methods,
                    description,
                    cb,
                } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .web_route(route, methods, description, move |input, ctx| {
                            let callback = callback.clone();
                            invoke_py_with_ctx(callback, input, ctx)
                        })
                        .map_err(py_err)?;
                }
                RegOp::ValidateConfig { cb } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .validate_config(move |cfg| {
                            let callback = callback.clone();
                            async move {
                                let cfg_json = serde_json::to_string(&cfg)
                                    .map_err(|e| WireError::invalid_config(e.to_string()))?;
                                callback.call_one_json(cfg_json).await.map(|_| ())
                            }
                        })
                        .map_err(py_err)?;
                }
                RegOp::ConfigChanged { cb } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .on_config_changed(move |prev, curr| {
                            let callback = callback.clone();
                            async move {
                                let payload =
                                    serde_json::json!({ "previous": prev, "current": curr });
                                let payload_json = serde_json::to_string(&payload)
                                    .map_err(|e| WireError::runtime_error(e.to_string()))?;
                                callback.call_one_json(payload_json).await.map(|_| ())
                            }
                        })
                        .map_err(py_err)?;
                }
                RegOp::Start { cb } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .on_start(move |ctx: StartupContext| {
                            let callback = callback.clone();
                            async move {
                                let cfg_json = serde_json::to_string(&ctx.config)
                                    .map_err(|e| WireError::runtime_error(e.to_string()))?;
                                let py_ctx = Python::attach(|py| {
                                    Py::new(
                                        py,
                                        PyStartupContext {
                                            inner: ctx,
                                            config_json: cfg_json,
                                        },
                                    )
                                })
                                .map_err(to_wire_error)?;
                                callback
                                    .call_two("{}".to_owned(), py_ctx.into_any())
                                    .await
                                    .map(|_| ())
                            }
                        })
                        .map_err(py_err)?;
                }
                RegOp::Stop { cb } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .on_stop(move |reason: PluginStopReason| {
                            let callback = callback.clone();
                            async move {
                                let reason_json = serde_json::to_string(&reason)
                                    .map_err(|e| WireError::runtime_error(e.to_string()))?;
                                callback.call_one_json(reason_json).await.map(|_| ())
                            }
                        })
                        .map_err(py_err)?;
                }
                RegOp::Health { cb } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .on_health(move || {
                            let callback = callback.clone();
                            async move { callback.call_zero().await }
                        })
                        .map_err(py_err)?;
                }
                RegOp::ToolDiscovery { cb } => {
                    let callback = AsyncCallback {
                        func: cb,
                        locals: locals.clone(),
                    };
                    worker
                        .on_tool_discovery(move |ctx: StartupContext| {
                            let callback = callback.clone();
                            async move {
                                let cfg_json = serde_json::to_string(&ctx.config)
                                    .map_err(|e| WireError::runtime_error(e.to_string()))?;
                                let py_ctx = Python::attach(|py| {
                                    Py::new(
                                        py,
                                        PyStartupContext {
                                            inner: ctx,
                                            config_json: cfg_json,
                                        },
                                    )
                                })
                                .map_err(to_wire_error)?;
                                let val = callback.call_two("{}".to_owned(), py_ctx.into_any()).await?;
                                let specs: Vec<astrbot_wire::DynamicToolSpec> =
                                    serde_json::from_value(val)
                                        .map_err(|e| WireError::invalid_request(e.to_string()))?;
                                Ok(specs
                                    .into_iter()
                                    .map(|spec| DiscoveredTool {
                                        spec,
                                        handler: Arc::new(|_, _| {
                                            Box::pin(async {
                                                Err(WireError::not_found(
                                                    "discovered Python tool requires Python dispatch",
                                                ))
                                            })
                                        }),
                                    })
                                    .collect())
                            }
                        })
                        .map_err(py_err)?;
                }
            }
        }
        Ok(worker)
    }
}

fn invoke_py_with_ctx(
    callback: AsyncCallback,
    input: Value,
    ctx: InvocationContext,
) -> BoxFuture<'static, Result<Value, WireError>> {
    Box::pin(async move {
        let input_json =
            serde_json::to_string(&input).map_err(|e| WireError::invalid_request(e.to_string()))?;
        let cfg_json = serde_json::to_string(&ctx.config)
            .map_err(|e| WireError::runtime_error(e.to_string()))?;
        let py_ctx = Python::attach(|py| {
            Py::new(
                py,
                PyInvocationContext {
                    inner: ctx,
                    config_json: cfg_json,
                },
            )
        })
        .map_err(to_wire_error)?;
        callback.call_two(input_json, py_ctx.into_any()).await
    })
}

#[pyfunction]
fn validate_config_schema_file_py(path: String) -> PyResult<String> {
    let val = validate_config_schema_file(&path).map_err(py_err)?;
    encode_json(&val)
}

#[pyfunction]
fn has_sensitive_schema_fields_py(schema_json: String) -> PyResult<bool> {
    let val = decode_json(&schema_json)?;
    Ok(has_sensitive_schema_fields(&val))
}

#[pyfunction]
fn load_manifest_json(plugin_root: String) -> PyResult<String> {
    let manifest = load_manifest(&plugin_root).map_err(py_err)?;
    serde_json::to_string(&manifest).map_err(|err| PyRuntimeError::new_err(err.to_string()))
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyHostClient>()?;
    m.add_class::<PyPluginClient>()?;
    m.add_class::<PyStartupContext>()?;
    m.add_class::<PyInvocationContext>()?;
    m.add_class::<PyDirectContext>()?;
    m.add_class::<PyNativeWorker>()?;
    m.add_function(wrap_pyfunction!(validate_config_schema_file_py, m)?)?;
    m.add_function(wrap_pyfunction!(has_sensitive_schema_fields_py, m)?)?;
    m.add_function(wrap_pyfunction!(load_manifest_json, m)?)?;
    Ok(())
}
