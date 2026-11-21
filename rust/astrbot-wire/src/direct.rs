//! Revocable direct-service wire types (`direct_service_v1`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const DIRECT_FEATURE: &str = "direct_service_v1";
pub const DIRECT_LISTEN: &str = "direct.listen";
pub const DIRECT_GRANT: &str = "direct.grant";
pub const DIRECT_REVOKE: &str = "direct.revoke";
pub const DIRECT_ATTACH: &str = "direct.attach";
pub const DIRECT_INVOKE: &str = "direct.invoke";

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceMode {
    #[default]
    HostScoped,
    Direct,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DirectListen {
    pub socket_path: String,
    pub provider_id: String,
    pub provider_generation: u64,
}

/// Credential-bearing grant; intentionally omits `Debug` so secrets never leak to logs.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DirectGrant {
    pub id: String,
    pub credential: String,
    pub caller_id: String,
    pub caller_generation: u64,
    pub provider_id: String,
    pub provider_generation: u64,
    pub publication_revision: u64,
    pub host_epoch: String,
    pub handler_id: String,
    pub expires_at: u64,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DirectRoute {
    pub socket_path: String,
    pub grant: DirectGrant,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ServiceRoute {
    pub direct: Option<DirectRoute>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RevokeGrant {
    pub id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DirectInvoke {
    pub input: Value,
}
