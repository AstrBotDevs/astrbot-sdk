use astrbot_worker::{CommandDefinition, ToolDefinition, Worker, WorkerError};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct TemplateConfig {
    #[serde(default = "default_greeting")]
    pub greeting: String,
}

fn default_greeting() -> String {
    "Hello from Rust!".to_owned()
}

/// Build the Worker with typed config validation, a command, and an LLM tool.
pub fn build_worker(plugin_root: impl AsRef<Path>) -> Result<Worker, WorkerError> {
    let mut worker = Worker::from_dir(plugin_root)?;

    worker.validate_config(|cfg| async move {
        let parsed: TemplateConfig = serde_json::from_value(if cfg.is_null() {
            json!({})
        } else {
            cfg
        })
        .map_err(|err| WorkerError::invalid_config(err.to_string()))?;
        if parsed.greeting.trim().is_empty() {
            return Err(WorkerError::invalid_config("greeting cannot be empty"));
        }
        Ok(())
    })?;

    worker.command_with(
        CommandDefinition {
            path: "hello".to_owned(),
            aliases: Vec::new(),
            description: Some("Reply with the configured greeting".to_owned()),
            priority: 0,
        },
        |_input, ctx| async move {
            let cfg: TemplateConfig = serde_json::from_value(if ctx.current_config()?.is_null() {
                json!({})
            } else {
                ctx.current_config()?
            })
            .map_err(|err| WorkerError::invalid_config(err.to_string()))?;
            Ok(json!({
                "type": "message",
                "propagation": "continue",
                "quote": false,
                "message": [{"type": "Plain", "text": cfg.greeting}]
            }))
        },
    )?;

    worker.tool(
        ToolDefinition::new(
            "greet",
            "Return the plugin greeting",
            json!({
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": []
            }),
        ),
        |args, ctx| async move {
            let cfg: TemplateConfig = serde_json::from_value(if ctx.current_config()?.is_null() {
                json!({})
            } else {
                ctx.current_config()?
            })
            .map_err(|err| WorkerError::invalid_config(err.to_string()))?;
            let who = args.get("name").and_then(Value::as_str).unwrap_or("world");
            Ok(json!({"greeting": format!("{} ({who})", cfg.greeting)}))
        },
    )?;

    Ok(worker)
}
