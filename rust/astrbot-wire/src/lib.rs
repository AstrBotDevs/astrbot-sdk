//! AstrBot plugin wire contract: protocol-v1 DTOs, bounded framing,
//! bidirectional cancellable peer, resumable streams, and direct service grants.
//!
//! This crate has zero dependencies on the AstrBot Host runtime.

pub mod direct;
pub mod framing;
pub mod manifest;
pub mod peer;
pub mod reconnect;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;

pub use direct::{
    DirectGrant, DirectInvoke, DirectListen, DirectRoute, RevokeGrant, ServiceMode, ServiceRoute,
    DIRECT_ATTACH, DIRECT_FEATURE, DIRECT_GRANT, DIRECT_INVOKE, DIRECT_LISTEN, DIRECT_REVOKE,
};
pub use manifest::{
    has_sensitive_schema_fields, load_manifest, reject_external_schema_refs,
    validate_config_schema_file, CapabilityDeclarations, CapabilityRequirement, PluginManifest,
    MAX_CONFIG_SCHEMA_BYTES,
};

pub const PROTOCOL_VERSION: u16 = 1;
pub const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_REQUEST_ID_BYTES: usize = 128;

// Feature flags negotiated during initialize / attach.
pub const CANCEL_FEATURE: &str = "cancel_v1";
pub const SCOPED_HOST_FEATURE: &str = "scoped_host_v1";
pub const LIVE_CONFIG_FEATURE: &str = "live_config_v1";
pub const CAPABILITY_UPDATE_FEATURE: &str = "capability_update_v1";
pub const PLUGIN_SERVICE_FEATURE: &str = "plugin_service_v1";
pub const DIRECT_SERVICE_FEATURE: &str = DIRECT_FEATURE;
pub const DYNAMIC_TOOLS_FEATURE: &str = "dynamic_tools_v1";
pub const EXTERNAL_ATTACH_FEATURE: &str = "external_attach_v1";
pub const RESUME_STREAM_FEATURE: &str = "resume_stream_v1";

// Protocol method names.
pub const METHOD_PING: &str = "ping";
pub const METHOD_INITIALIZE: &str = "initialize";
pub const METHOD_SHUTDOWN: &str = "shutdown";
pub const METHOD_INVOKE: &str = "invoke";
pub const METHOD_RESUMABLE_INVOKE: &str = "resumable.invoke";
pub const METHOD_INVOKE_TOOL: &str = "invoke_tool";
pub const METHOD_INVOKE_HOOK: &str = "invoke_hook";
pub const METHOD_INVOKE_CRON: &str = "invoke_cron";
pub const METHOD_INVOKE_SERVICE: &str = "invoke_service";
pub const METHOD_VALIDATE_CONFIG: &str = "validate_config";
pub const METHOD_CONFIGURE: &str = "configure";
pub const METHOD_CAPABILITIES_UPDATE: &str = "capabilities.update";
pub const METHOD_TOOLS_DISCOVER: &str = "tools.discover";
pub const METHOD_TOOLS_INVALIDATE: &str = "tools.invalidate";
pub const METHOD_CAPABILITY_INVOKE: &str = "capability.invoke";
pub const METHOD_PLUGIN_SERVICE_INVOKE: &str = "plugin.service.invoke";
pub const METHOD_ATTACH: &str = "attach";
pub const METHOD_RESUME_ATTACH: &str = "attach.resume";
pub const METHOD_STREAM_CONSUMED: &str = "stream.consumed";

/// Validated non-empty request or invocation identifier (1..=128 UTF-8 bytes).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(transparent)]
pub struct RequestId(String);

impl RequestId {
    pub fn new(value: impl Into<String>) -> Result<Self, WireError> {
        let value = value.into();
        if value.is_empty() || value.len() > MAX_REQUEST_ID_BYTES {
            return Err(WireError::invalid_request(
                "request ID must contain 1..=128 UTF-8 bytes",
            ));
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl<'de> Deserialize<'de> for RequestId {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::new(String::deserialize(deserializer)?).map_err(serde::de::Error::custom)
    }
}

/// Stable protocol and runtime error preserving machine error codes.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, thiserror::Error)]
#[error("{message} ({code})")]
pub struct WireError {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl WireError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            data: None,
        }
    }

    pub fn with_data(code: impl Into<String>, message: impl Into<String>, data: Value) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            data: Some(data),
        }
    }

    pub fn invalid_request(message: impl Into<String>) -> Self {
        Self::new("INVALID_REQUEST", message)
    }

    pub fn invalid_config(message: impl Into<String>) -> Self {
        Self::new("INVALID_CONFIG", message)
    }

    pub fn capability_denied(message: impl Into<String>) -> Self {
        Self::new("CAPABILITY_DENIED", message)
    }

    pub fn call_scope_retired(message: impl Into<String>) -> Self {
        Self::new("CALL_SCOPE_RETIRED", message)
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new("NOT_FOUND", message)
    }

    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new("CONFLICT", message)
    }

    pub fn host_unavailable(message: impl Into<String>) -> Self {
        Self::new("HOST_UNAVAILABLE", message)
    }

    pub fn cancelled(message: impl Into<String>) -> Self {
        Self::new("CANCELLED", message)
    }

    pub fn runtime_error(message: impl Into<String>) -> Self {
        Self::new("PLUGIN_RUNTIME_ERROR", message)
    }
}

/// Six protocol-v1 JSON frame types matching `astrbot_sdk.protocol.frames`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ProtocolFrame {
    Request {
        id: RequestId,
        method: String,
        #[serde(default)]
        params: Map<String, Value>,
    },
    Response {
        id: RequestId,
        #[serde(default)]
        result: Value,
    },
    Yield {
        id: RequestId,
        sequence: u64,
        #[serde(default)]
        result: Value,
    },
    Ack {
        id: RequestId,
        sequence: u64,
    },
    Cancel {
        id: RequestId,
    },
    Error {
        id: RequestId,
        code: String,
        message: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        data: Option<Value>,
    },
}

impl ProtocolFrame {
    pub fn id(&self) -> &RequestId {
        match self {
            Self::Request { id, .. }
            | Self::Response { id, .. }
            | Self::Yield { id, .. }
            | Self::Ack { id, .. }
            | Self::Cancel { id }
            | Self::Error { id, .. } => id,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilityGrant {
    pub id: String,
    #[serde(default)]
    pub scope: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HandlerDescriptor {
    pub id: String,
    pub kind: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub priority: i64,
    #[serde(default)]
    pub details: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServiceDescriptor {
    pub handler_id: String,
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub mode: ServiceMode,
    #[serde(default)]
    pub input_schema: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolParamSpec {
    pub name: String,
    #[serde(rename = "type")]
    pub param_type: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "default_true")]
    pub required: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DynamicToolSpec {
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub params: Vec<ToolParamSpec>,
    #[serde(default)]
    pub parameters_schema: Option<Value>,
    #[serde(default)]
    pub resumable: bool,
    #[serde(default)]
    pub streaming_input: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PluginSummary {
    pub id: String,
    pub name: String,
    pub version: String,
    pub schema_version: u16,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InitializeParams {
    pub protocol_versions: Vec<u16>,
    #[serde(default)]
    pub config: Value,
    #[serde(default)]
    pub host: Map<String, Value>,
    #[serde(default)]
    pub capabilities: Vec<CapabilityGrant>,
    #[serde(default)]
    pub features: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct InitializeResult {
    pub protocol_version: u16,
    pub plugin: PluginSummary,
    #[serde(default)]
    pub handlers: Vec<HandlerDescriptor>,
    #[serde(default)]
    pub web_routes: Vec<Value>,
    #[serde(default)]
    pub services: Vec<ServiceDescriptor>,
    #[serde(default)]
    pub capabilities: Vec<String>,
    #[serde(default)]
    pub features: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PluginStopReason {
    Reload,
    Disabled,
    Shutdown,
    StartupFailed,
    ConnectionClosed,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExternalCredential {
    pub plugin_id: String,
    pub credential: String,
    pub socket_path: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttachParams {
    pub plugin_id: String,
    pub credential: String,
    #[serde(default = "default_protocol_version")]
    pub protocol_version: u16,
    #[serde(default)]
    pub features: Vec<String>,
}

fn default_protocol_version() -> u16 {
    PROTOCOL_VERSION
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResumeAttachParams {
    pub attach: AttachParams,
    pub resume_token: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttachedResult {
    pub connection_id: String,
    pub host_epoch: String,
    #[serde(default)]
    pub resume_token: Option<String>,
    #[serde(default)]
    pub features: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StreamConsumed {
    pub id: RequestId,
    pub sequence: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolDiscoveryResult {
    pub version: u64,
    #[serde(default)]
    pub tools: Vec<DynamicToolSpec>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServiceMap(pub BTreeMap<String, Value>);

pub fn default_worker_features() -> Vec<String> {
    vec![
        CANCEL_FEATURE.to_owned(),
        SCOPED_HOST_FEATURE.to_owned(),
        LIVE_CONFIG_FEATURE.to_owned(),
        CAPABILITY_UPDATE_FEATURE.to_owned(),
        PLUGIN_SERVICE_FEATURE.to_owned(),
        DIRECT_SERVICE_FEATURE.to_owned(),
        DYNAMIC_TOOLS_FEATURE.to_owned(),
        EXTERNAL_ATTACH_FEATURE.to_owned(),
        RESUME_STREAM_FEATURE.to_owned(),
    ]
}
