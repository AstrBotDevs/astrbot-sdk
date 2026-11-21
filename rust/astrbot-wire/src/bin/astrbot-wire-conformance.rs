//! Protocol v1 conformance CLI: spawns any Worker executable over stdio and
//! verifies ping, initialize handshake, frame schema compliance, and shutdown.

use astrbot_wire::{
    default_worker_features, framing, InitializeParams, InitializeResult, ProtocolFrame, RequestId,
    METHOD_INITIALIZE, METHOD_PING, METHOD_SHUTDOWN, PROTOCOL_VERSION,
};
use serde_json::{json, Map};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{BufReader, BufWriter};
use tokio::process::Command;

const FRAME_SCHEMA_STR: &str = include_str!("../../schema/v1/frame.schema.json");

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let mut expected_id: Option<String> = None;
    let mut command: Vec<String> = Vec::new();
    while let Some(arg) = args.next() {
        if arg == "--id" {
            expected_id = Some(args.next().ok_or("expected plugin id after --id")?);
        } else if arg == "--" {
            command.extend(args);
            break;
        } else {
            command.push(arg);
            command.extend(args);
            break;
        }
    }
    if command.is_empty() {
        return Err("usage: astrbot-wire-conformance [--id <plugin_id>] -- <cmd> [args...]".into());
    }

    let schema_val: serde_json::Value = serde_json::from_str(FRAME_SCHEMA_STR)?;
    let validator = jsonschema::validator_for(&schema_val)?;

    let mut child = Command::new(&command[0])
        .args(&command[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()?;

    let mut writer = BufWriter::new(child.stdin.take().ok_or("missing stdin")?);
    let mut reader = BufReader::new(child.stdout.take().ok_or("missing stdout")?);

    // 1. Ping probe
    let ping_req = ProtocolFrame::Request {
        id: RequestId::new("conf:ping")?,
        method: METHOD_PING.to_owned(),
        params: Map::new(),
    };
    validate_frame(&validator, &ping_req)?;
    framing::write_frame(&mut writer, &ping_req).await?;
    let ping_reply =
        tokio::time::timeout(Duration::from_secs(10), framing::read_frame(&mut reader))
            .await??
            .ok_or("EOF during ping")?;
    validate_frame(&validator, &ping_reply)?;
    match ping_reply {
        ProtocolFrame::Response { id, .. } if id.as_str() == "conf:ping" => {}
        other => return Err(format!("unexpected ping reply: {other:?}").into()),
    }

    // 2. Initialize handshake
    let init_params = InitializeParams {
        protocol_versions: vec![PROTOCOL_VERSION],
        config: json!({}),
        host: Map::new(),
        capabilities: vec![],
        features: default_worker_features(),
    };
    let params_map = serde_json::to_value(init_params)?
        .as_object()
        .cloned()
        .unwrap_or_default();
    let init_req = ProtocolFrame::Request {
        id: RequestId::new("conf:init")?,
        method: METHOD_INITIALIZE.to_owned(),
        params: params_map,
    };
    validate_frame(&validator, &init_req)?;
    framing::write_frame(&mut writer, &init_req).await?;
    let init_reply =
        tokio::time::timeout(Duration::from_secs(15), framing::read_frame(&mut reader))
            .await??
            .ok_or("EOF during initialize")?;
    validate_frame(&validator, &init_reply)?;
    let init_result: InitializeResult = match init_reply {
        ProtocolFrame::Response { id, result } if id.as_str() == "conf:init" => {
            serde_json::from_value(result)?
        }
        other => return Err(format!("unexpected initialize reply: {other:?}").into()),
    };
    if init_result.protocol_version != PROTOCOL_VERSION {
        return Err(format!(
            "unsupported protocol_version {}",
            init_result.protocol_version
        )
        .into());
    }
    if let Some(expected) = expected_id {
        if init_result.plugin.id != expected && init_result.plugin.name != expected {
            return Err(format!(
                "expected plugin {expected}, got id={} name={}",
                init_result.plugin.id, init_result.plugin.name
            )
            .into());
        }
    }

    // 3. Graceful shutdown
    let stop_req = ProtocolFrame::Request {
        id: RequestId::new("conf:stop")?,
        method: METHOD_SHUTDOWN.to_owned(),
        params: Map::new(),
    };
    validate_frame(&validator, &stop_req)?;
    framing::write_frame(&mut writer, &stop_req).await?;
    let stop_reply =
        tokio::time::timeout(Duration::from_secs(10), framing::read_frame(&mut reader))
            .await??
            .ok_or("EOF during shutdown")?;
    validate_frame(&validator, &stop_reply)?;
    match stop_reply {
        ProtocolFrame::Response { id, .. } if id.as_str() == "conf:stop" => {}
        other => return Err(format!("unexpected shutdown reply: {other:?}").into()),
    }
    drop(writer);
    drop(reader);

    let status = tokio::time::timeout(Duration::from_secs(10), child.wait()).await??;
    if !status.success() {
        return Err(format!("worker exited with non-zero status: {status}").into());
    }
    println!(
        "conformance ok: plugin={} version={} handlers={} services={}",
        init_result.plugin.id,
        init_result.plugin.version,
        init_result.handlers.len(),
        init_result.services.len(),
    );
    Ok(())
}

fn validate_frame(
    validator: &jsonschema::Validator,
    frame: &ProtocolFrame,
) -> Result<(), Box<dyn std::error::Error>> {
    let val = serde_json::to_value(frame)?;
    if let Err(err) = validator.validate(&val) {
        return Err(format!("frame failed JSON schema: {err}").into());
    }
    Ok(())
}
