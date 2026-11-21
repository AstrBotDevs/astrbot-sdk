//! Static plugin manifest (`metadata.yaml` v2 / `plugin.json` v1) and offline
//! `config.schema.json` validation.

use crate::WireError;
use semver::{Version, VersionReq};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

pub const MAX_CONFIG_SCHEMA_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CapabilityRequirement {
    pub id: String,
    #[serde(default)]
    pub scope: Map<String, Value>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CapabilityDeclarations {
    #[serde(default)]
    pub required: Vec<CapabilityRequirement>,
    #[serde(default)]
    pub optional: Vec<CapabilityRequirement>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PluginManifest {
    pub schema_version: u16,
    pub id: String,
    pub name: String,
    pub description: String,
    pub version: String,
    pub author: String,
    pub language: String,
    pub entrypoint: Option<String>,
    pub command: Vec<String>,
    pub sdk_version: Option<String>,
    pub host_version: Option<String>,
    pub target: Option<String>,
    pub capabilities: CapabilityDeclarations,
    pub dependencies: BTreeMap<String, String>,
    pub optional_dependencies: BTreeMap<String, String>,
}

impl PluginManifest {
    pub fn parsed_version(&self) -> Result<Version, WireError> {
        Version::parse(&self.version).map_err(|err| {
            WireError::new("INVALID_PLUGIN_METADATA", format!("invalid version: {err}"))
        })
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawPluginJson {
    schema_version: u16,
    id: String,
    name: String,
    description: String,
    version: String,
    author: String,
    command: Vec<String>,
    #[serde(default)]
    host_version: Option<String>,
    #[serde(default)]
    target: Option<String>,
    #[serde(default)]
    capabilities: CapabilityDeclarations,
    #[serde(default)]
    dependencies: BTreeMap<String, String>,
    #[serde(default)]
    optional_dependencies: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct RawRuntimeYaml {
    api: Option<String>,
    entrypoint: Option<String>,
    sdk_version: Option<String>,
    language: Option<String>,
    #[serde(default)]
    command: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct RawMetadataYaml {
    schema_version: Option<u16>,
    name: Option<String>,
    desc: Option<String>,
    description: Option<String>,
    version: Option<String>,
    author: Option<String>,
    astrbot_version: Option<String>,
    target: Option<String>,
    runtime: Option<RawRuntimeYaml>,
    #[serde(default)]
    capabilities: CapabilityDeclarations,
    #[serde(default)]
    dependencies: BTreeMap<String, String>,
    #[serde(default)]
    optional_dependencies: BTreeMap<String, String>,
}

fn validate_command_argv(plugin_root: &Path, command: &[String]) -> Result<(), WireError> {
    if command.is_empty() {
        return Err(WireError::new(
            "INVALID_PLUGIN_METADATA",
            "command must contain at least one element",
        ));
    }
    if command.iter().any(|arg| arg.trim().is_empty()) {
        return Err(WireError::new(
            "INVALID_PLUGIN_METADATA",
            "command arguments must be non-empty strings",
        ));
    }
    let exe = Path::new(&command[0]);
    if exe.is_absolute()
        || exe
            .components()
            .any(|component| matches!(component, Component::ParentDir))
    {
        return Err(WireError::new(
            "INVALID_PLUGIN_METADATA",
            "command executable must be a relative path inside the plugin root",
        ));
    }
    let joined = plugin_root.join(exe);
    let root_norm = plugin_root
        .canonicalize()
        .unwrap_or_else(|_| plugin_root.to_path_buf());
    if joined.exists() {
        let exe_norm = joined
            .canonicalize()
            .map_err(|err| WireError::new("INVALID_PLUGIN_METADATA", err.to_string()))?;
        if !exe_norm.starts_with(&root_norm) {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                "command executable escapes the plugin root",
            ));
        }
    }
    Ok(())
}

fn validate_capabilities(caps: &CapabilityDeclarations) -> Result<(), WireError> {
    let mut seen = BTreeSet::new();
    for item in caps.required.iter().chain(caps.optional.iter()) {
        if !is_valid_capability_id(&item.id) {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                format!("invalid capability id: {:?}", item.id),
            ));
        }
        if !seen.insert(item.id.clone()) {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                format!("duplicate capability declaration: {}", item.id),
            ));
        }
    }
    Ok(())
}

fn is_valid_capability_id(id: &str) -> bool {
    let mut parts = id.split('.');
    let Some(first) = parts.next() else {
        return false;
    };
    let rest: Vec<&str> = parts.collect();
    if rest.is_empty() {
        return false;
    }
    std::iter::once(first).chain(rest).all(|part| {
        let mut chars = part.chars();
        matches!(chars.next(), Some(c) if c.is_ascii_lowercase())
            && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
    })
}

fn validate_dependencies(
    plugin_id: &str,
    plugin_name: &str,
    required: &BTreeMap<String, String>,
    optional: &BTreeMap<String, String>,
) -> Result<(), WireError> {
    for (dep_id, range) in required.iter().chain(optional.iter()) {
        if dep_id.trim().is_empty() {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                "dependency plugin id must be non-empty",
            ));
        }
        if dep_id == plugin_id || dep_id == plugin_name {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                format!("plugin cannot depend on itself: {dep_id}"),
            ));
        }
        if required.contains_key(dep_id) && optional.contains_key(dep_id) {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                format!("dependency cannot be both required and optional: {dep_id}"),
            ));
        }
        VersionReq::parse(range).map_err(|err| {
            WireError::new(
                "INVALID_PLUGIN_METADATA",
                format!("invalid SemVer range for dependency {dep_id}: {err}"),
            )
        })?;
    }
    Ok(())
}

/// Load a static plugin manifest from `plugin.json` or `metadata.yaml` under `plugin_root`.
pub fn load_manifest(plugin_root: impl AsRef<Path>) -> Result<PluginManifest, WireError> {
    let root = plugin_root.as_ref();
    let json_path = root.join("plugin.json");
    let yaml_path = {
        let p = root.join("metadata.yaml");
        if p.is_file() {
            Some(p)
        } else {
            let yml = root.join("metadata.yml");
            yml.is_file().then_some(yml)
        }
    };

    if json_path.is_file() {
        let raw_text = std::fs::read_to_string(&json_path)
            .map_err(|err| WireError::new("INVALID_PLUGIN_METADATA", err.to_string()))?;
        let raw: RawPluginJson = serde_json::from_str(&raw_text)
            .map_err(|err| WireError::new("INVALID_PLUGIN_METADATA", err.to_string()))?;
        if raw.schema_version != 1 {
            return Err(WireError::new(
                "UNSUPPORTED_METADATA_VERSION",
                format!(
                    "unsupported plugin.json schemaVersion: {}",
                    raw.schema_version
                ),
            ));
        }
        if raw.id.trim().is_empty()
            || raw.name.trim().is_empty()
            || raw.author.trim().is_empty()
            || raw.version.trim().is_empty()
        {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                "plugin.json id, name, author and version must be non-empty",
            ));
        }
        validate_command_argv(root, &raw.command)?;
        validate_capabilities(&raw.capabilities)?;
        validate_dependencies(
            &raw.id,
            &raw.name,
            &raw.dependencies,
            &raw.optional_dependencies,
        )?;
        let manifest = PluginManifest {
            schema_version: raw.schema_version,
            id: raw.id,
            name: raw.name,
            description: raw.description,
            version: raw.version,
            author: raw.author,
            language: "rust".to_owned(),
            entrypoint: None,
            command: raw.command,
            sdk_version: None,
            host_version: raw.host_version,
            target: raw.target,
            capabilities: raw.capabilities,
            dependencies: raw.dependencies,
            optional_dependencies: raw.optional_dependencies,
        };
        manifest.parsed_version()?;
        return Ok(manifest);
    }

    if let Some(yaml_path) = yaml_path {
        let raw_text = std::fs::read_to_string(&yaml_path)
            .map_err(|err| WireError::new("INVALID_PLUGIN_METADATA", err.to_string()))?;
        let raw: RawMetadataYaml = serde_yaml::from_str(&raw_text)
            .map_err(|err| WireError::new("INVALID_PLUGIN_METADATA", err.to_string()))?;
        if raw.schema_version != Some(2) {
            return Err(WireError::new(
                "UNSUPPORTED_METADATA_VERSION",
                format!(
                    "expected metadata.yaml schema_version 2, got {:?}",
                    raw.schema_version
                ),
            ));
        }
        let runtime = raw.runtime.ok_or_else(|| {
            WireError::new("INVALID_PLUGIN_METADATA", "metadata.yaml requires runtime")
        })?;
        if runtime.api.as_deref() != Some("sdk") {
            return Err(WireError::new(
                "INVALID_PLUGIN_METADATA",
                "metadata.yaml v2 requires runtime.api == 'sdk'",
            ));
        }
        let name = raw
            .name
            .filter(|v| !v.trim().is_empty())
            .ok_or_else(|| WireError::new("INVALID_PLUGIN_METADATA", "name must be non-empty"))?;
        let author = raw
            .author
            .filter(|v| !v.trim().is_empty())
            .ok_or_else(|| WireError::new("INVALID_PLUGIN_METADATA", "author must be non-empty"))?;
        let version = raw
            .version
            .filter(|v| !v.trim().is_empty())
            .ok_or_else(|| {
                WireError::new("INVALID_PLUGIN_METADATA", "version must be non-empty")
            })?;
        let description = raw.desc.or(raw.description).unwrap_or_default();
        let language = runtime.language.unwrap_or_else(|| "python".to_owned());
        if !runtime.command.is_empty() {
            validate_command_argv(root, &runtime.command)?;
        }
        validate_capabilities(&raw.capabilities)?;
        let id = format!(
            "{}/{}",
            author.to_ascii_lowercase().replace('/', "_"),
            name.to_ascii_lowercase().replace('/', "_")
        );
        validate_dependencies(&id, &name, &raw.dependencies, &raw.optional_dependencies)?;
        let manifest = PluginManifest {
            schema_version: 2,
            id,
            name,
            description,
            version,
            author,
            language,
            entrypoint: runtime.entrypoint,
            command: runtime.command,
            sdk_version: runtime.sdk_version,
            host_version: raw.astrbot_version,
            target: raw.target,
            capabilities: raw.capabilities,
            dependencies: raw.dependencies,
            optional_dependencies: raw.optional_dependencies,
        };
        manifest.parsed_version()?;
        return Ok(manifest);
    }

    Err(WireError::new(
        "INVALID_PLUGIN_METADATA",
        format!(
            "plugin manifest not found in {}; expected plugin.json or metadata.yaml",
            PathBuf::from(root).display()
        ),
    ))
}

/// Ensure a JSON Schema is offline-resolvable (no external `$ref`) and declares an object root.
pub fn reject_external_schema_refs(schema: &Value) -> Result<(), WireError> {
    fn walk(root: &Value, node: &Value) -> Result<(), WireError> {
        match node {
            Value::Object(map) => {
                if let Some(reference) = map.get("$ref") {
                    let Some(pointer) = reference.as_str() else {
                        return Err(WireError::invalid_config("schema $ref must be a string"));
                    };
                    if !pointer.starts_with("#/") {
                        return Err(WireError::invalid_config(format!(
                            "external or non-pointer schema $ref is not allowed: {pointer}"
                        )));
                    }
                    let json_pointer = &pointer[1..];
                    if root.pointer(json_pointer).is_none() {
                        return Err(WireError::invalid_config(format!(
                            "unresolved internal schema $ref: {pointer}"
                        )));
                    }
                }
                for value in map.values() {
                    walk(root, value)?;
                }
            }
            Value::Array(items) => {
                for item in items {
                    walk(root, item)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    let obj = schema
        .as_object()
        .ok_or_else(|| WireError::invalid_config("config schema root must be a JSON object"))?;
    walk(schema, schema)?;

    let is_object_root = obj.get("type").and_then(Value::as_str) == Some("object")
        || obj
            .get("$ref")
            .and_then(Value::as_str)
            .and_then(|ptr| ptr.strip_prefix('#'))
            .and_then(|ptr| schema.pointer(ptr))
            .and_then(|target| target.get("type"))
            .and_then(Value::as_str)
            == Some("object");
    if !is_object_root {
        return Err(WireError::invalid_config(
            "config.schema.json root must declare type: \"object\"",
        ));
    }
    Ok(())
}

/// Return true when any property or subschema marks `writeOnly: true` or `format: "password"`.
pub fn has_sensitive_schema_fields(schema: &Value) -> bool {
    match schema {
        Value::Object(map) => {
            if map.get("writeOnly").and_then(Value::as_bool) == Some(true) {
                return true;
            }
            if map.get("format").and_then(Value::as_str) == Some("password") {
                return true;
            }
            map.values().any(has_sensitive_schema_fields)
        }
        Value::Array(items) => items.iter().any(has_sensitive_schema_fields),
        _ => false,
    }
}

/// Validate a `config.schema.json` file on disk and return the parsed schema.
pub fn validate_config_schema_file(path: impl AsRef<Path>) -> Result<Value, WireError> {
    let raw = std::fs::read(path.as_ref()).map_err(|err| {
        WireError::invalid_config(format!("cannot read config.schema.json: {err}"))
    })?;
    if raw.len() > MAX_CONFIG_SCHEMA_BYTES {
        return Err(WireError::invalid_config(
            "config.schema.json exceeds 64 KiB limit",
        ));
    }
    let schema: Value = serde_json::from_slice(&raw)
        .map_err(|err| WireError::invalid_config(format!("invalid config.schema.json: {err}")))?;
    reject_external_schema_refs(&schema)?;
    jsonschema::validator_for(&schema)
        .map_err(|err| WireError::invalid_config(format!("invalid JSON Schema: {err}")))?;
    Ok(schema)
}
