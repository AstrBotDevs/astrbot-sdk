//! Standalone Rust Worker echo plugin speaking AstrBot protocol-v1 over stdio.

use astrbot_wire::load_manifest;
use astrbot_worker::{CommandDefinition, ServiceDefinition, ToolDefinition, Worker, WorkerError};
use serde_json::{json, Value};
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
    let manifest = load_manifest(&manifest_dir)?;

    let mut worker = Worker::from_manifest(manifest);
    worker.validate_config(|cfg| async move {
        if let Some(obj) = cfg.as_object() {
            if obj.get("prefix").is_some_and(|v| !v.is_string()) {
                return Err(WorkerError::invalid_config("prefix must be a string"));
            }
        }
        Ok(())
    })?;

    worker.command_with(
        CommandDefinition {
            path: "echo".to_owned(),
            aliases: vec!["rust_echo".to_owned()],
            description: Some("Echo text from the Rust Worker".to_owned()),
            priority: 0,
        },
        |input, ctx| async move {
            let cfg = ctx.current_config()?;
            let prefix = cfg.get("prefix").and_then(Value::as_str).unwrap_or("");
            let text = input
                .get("args")
                .and_then(|a| a.get(1))
                .and_then(|ev| ev.get("value"))
                .and_then(|ev| ev.get("message"))
                .and_then(Value::as_array)
                .and_then(|arr| arr.first())
                .and_then(|seg| seg.get("text"))
                .and_then(Value::as_str)
                .unwrap_or("echo");
            Ok(json!({
                "type": "message",
                "propagation": "continue",
                "quote": false,
                "message": [{"type": "Plain", "text": format!("{prefix}{text}")}]
            }))
        },
    )?;

    worker.tool(
        ToolDefinition::new(
            "rust_echo",
            "Echo text via native Rust tool",
            json!({
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"]
            }),
        ),
        |args, ctx| async move {
            let cfg = ctx.current_config()?;
            let prefix = cfg.get("prefix").and_then(Value::as_str).unwrap_or("");
            let text = args.get("text").and_then(Value::as_str).unwrap_or("");
            Ok(json!({ "text": format!("{prefix}{text}") }))
        },
    )?;

    worker.service(
        ServiceDefinition::new("echo", "Cross-plugin echo service"),
        |input, ctx| async move {
            let cfg = ctx.current_config()?;
            let prefix = cfg.get("prefix").and_then(Value::as_str).unwrap_or("");
            let text = input.get("text").and_then(Value::as_str).unwrap_or("");
            Ok(json!({ "echo": format!("{prefix}{text}") }))
        },
    )?;

    worker.run_stdio().await?;
    Ok(())
}
