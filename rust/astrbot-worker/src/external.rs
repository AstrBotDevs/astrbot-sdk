//! External Worker transports: Unix Domain Socket (`attach` / `attach.resume`)
//! and WebSocket dial-out (`Authorization: Bearer <token>`).

use crate::Worker;
use astrbot_wire::{
    default_worker_features, framing, peer::Peer, reconnect::DEFAULT_RESUME_GRACE, AttachParams,
    AttachedResult, ExternalCredential, ProtocolFrame, RequestId, ResumeAttachParams, WireError,
    METHOD_ATTACH, METHOD_RESUME_ATTACH, PROTOCOL_VERSION,
};
use futures::{SinkExt, StreamExt};
use serde_json::Value;
use std::{path::Path, time::Duration};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio_tungstenite::tungstenite::{
    client::IntoClientRequest,
    http::{header::AUTHORIZATION, HeaderValue},
    Message as WsMessage,
};

/// Load an `ExternalCredential` from disk, rejecting overly permissive Unix permissions.
pub fn load_external_credential(path: impl AsRef<Path>) -> Result<ExternalCredential, WireError> {
    let path = path.as_ref();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let meta = std::fs::symlink_metadata(path)
            .map_err(|err| WireError::invalid_request(format!("cannot stat credential: {err}")))?;
        if meta.file_type().is_symlink() {
            return Err(WireError::invalid_request(
                "external credential file must not be a symlink",
            ));
        }
        if meta.permissions().mode() & 0o077 != 0 {
            return Err(WireError::invalid_request(
                "external credential file must have 0600 permissions",
            ));
        }
    }
    let raw = std::fs::read_to_string(path)
        .map_err(|err| WireError::invalid_request(format!("cannot read credential: {err}")))?;
    serde_json::from_str(&raw)
        .map_err(|err| WireError::invalid_request(format!("invalid credential JSON: {err}")))
}

#[cfg(unix)]
async fn attach_uds(
    credential: &ExternalCredential,
    resume_token: Option<&str>,
) -> Result<(tokio::net::UnixStream, AttachedResult), WireError> {
    let mut stream = tokio::net::UnixStream::connect(&credential.socket_path)
        .await
        .map_err(|err| WireError::host_unavailable(format!("UDS connect failed: {err}")))?;

    let attach = AttachParams {
        plugin_id: credential.plugin_id.clone(),
        credential: credential.credential.clone(),
        protocol_version: PROTOCOL_VERSION,
        features: default_worker_features(),
    };
    let (method, params_val) = if let Some(token) = resume_token {
        (
            METHOD_RESUME_ATTACH,
            serde_json::to_value(ResumeAttachParams {
                attach,
                resume_token: token.to_owned(),
            }),
        )
    } else {
        (METHOD_ATTACH, serde_json::to_value(attach))
    };
    let params = params_val
        .map_err(|err| WireError::invalid_request(err.to_string()))?
        .as_object()
        .cloned()
        .ok_or_else(|| WireError::invalid_request("invalid attach params"))?;

    let id = RequestId::new("worker:attach")?;
    framing::write_frame(
        &mut stream,
        &ProtocolFrame::Request {
            id: id.clone(),
            method: method.to_owned(),
            params,
        },
    )
    .await?;

    let mut reader = BufReader::new(&mut stream);
    let reply = framing::read_frame(&mut reader)
        .await?
        .ok_or_else(|| WireError::host_unavailable("EOF before attach response"))?;
    match reply {
        ProtocolFrame::Response {
            id: reply_id,
            result,
        } if reply_id == id => {
            let attached: AttachedResult = serde_json::from_value(result)
                .map_err(|err| WireError::invalid_request(err.to_string()))?;
            Ok((stream, attached))
        }
        ProtocolFrame::Error {
            code,
            message,
            data,
            ..
        } => Err(WireError {
            code,
            message,
            data,
        }),
        _ => Err(WireError::invalid_request("unexpected attach reply frame")),
    }
}

#[cfg(unix)]
pub(crate) async fn run_uds(
    worker: Worker,
    credential: ExternalCredential,
) -> Result<(), WireError> {
    if credential.plugin_id != worker.manifest.id && credential.plugin_id != worker.manifest.name {
        return Err(WireError::new(
            "IDENTITY_MISMATCH",
            "external credential belongs to another plugin",
        ));
    }

    let (stream, attached) =
        tokio::time::timeout(Duration::from_secs(5), attach_uds(&credential, None))
            .await
            .map_err(|_| WireError::new("DEADLINE_EXCEEDED", "external attach timed out"))??;
    let (reader, writer) = stream.into_split();
    let (owner, incoming) = if attached.resume_token.is_some() {
        Peer::start_reconnectable(reader, writer, false, DEFAULT_RESUME_GRACE)
    } else {
        Peer::start(reader, writer, false)
    };
    let peer = owner.peer();
    let work = worker.run_peer(owner, incoming);
    tokio::pin!(work);

    if attached.resume_token.is_none() {
        return work.await;
    }

    tokio::select! {
        biased;
        res = &mut work => res,
        res = reconnect_loop(&peer, &credential, &attached) => {
            peer.close();
            let stopped = work.await;
            res.and(stopped)
        }
    }
}

#[cfg(unix)]
async fn reconnect_loop(
    peer: &Peer,
    credential: &ExternalCredential,
    original: &AttachedResult,
) -> Result<(), WireError> {
    loop {
        peer.wait_disconnected().await;
        if peer.is_closed() {
            return Ok(());
        }
        let mut interval = tokio::time::interval(Duration::from_millis(250));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            let attempt = tokio::select! {
                _ = peer.wait_closed() => return Ok(()),
                res = async {
                    interval.tick().await;
                    tokio::time::timeout(
                        Duration::from_secs(2),
                        attach_uds(credential, original.resume_token.as_deref()),
                    )
                    .await
                    .map_err(|_| WireError::new("DEADLINE_EXCEEDED", "resume attach timed out"))?
                } => res,
            };
            match attempt {
                Ok((stream, attached)) => {
                    if attached.host_epoch != original.host_epoch
                        || attached.connection_id != original.connection_id
                        || attached.resume_token != original.resume_token
                    {
                        return Err(WireError::new(
                            "RESUME_REJECTED",
                            "original host session is no longer available",
                        ));
                    }
                    let (reader, writer) = stream.into_split();
                    peer.reconnect(reader, writer).await?;
                    break;
                }
                Err(err) if err.code == "CAPABILITY_DENIED" || err.code == "IDENTITY_MISMATCH" => {
                    return Err(err);
                }
                Err(err) => {
                    tracing::debug!(%err, "deferred external UDS reconnection");
                }
            }
        }
    }
}

pub(crate) async fn run_ws(
    worker: Worker,
    url: &str,
    token: Option<&str>,
) -> Result<(), WireError> {
    let mut request = url
        .into_client_request()
        .map_err(|err| WireError::invalid_request(format!("invalid websocket url: {err}")))?;
    if let Some(bearer) = token.filter(|t| !t.is_empty()) {
        let header = HeaderValue::from_str(&format!("Bearer {bearer}"))
            .map_err(|err| WireError::invalid_request(err.to_string()))?;
        request.headers_mut().insert(AUTHORIZATION, header);
    }

    let (ws_stream, _) = tokio_tungstenite::connect_async(request)
        .await
        .map_err(|err| WireError::host_unavailable(format!("websocket connect failed: {err}")))?;

    let (mut ws_sink, mut ws_source) = ws_stream.split();
    let (peer_side, bridge_side) = tokio::io::duplex(256 * 1024);
    let (peer_reader, peer_writer) = tokio::io::split(peer_side);
    let (bridge_reader, mut bridge_writer) = tokio::io::split(bridge_side);

    let inbound_pump = tokio::spawn(async move {
        while let Some(msg) = ws_source.next().await {
            match msg {
                Ok(WsMessage::Text(text)) => {
                    let mut bytes = text.into_bytes();
                    if !bytes.ends_with(b"\n") {
                        bytes.push(b'\n');
                    }
                    if bridge_writer.write_all(&bytes).await.is_err() {
                        break;
                    }
                }
                Ok(WsMessage::Binary(bin)) => {
                    let mut bytes = bin;
                    if !bytes.ends_with(b"\n") {
                        bytes.push(b'\n');
                    }
                    if bridge_writer.write_all(&bytes).await.is_err() {
                        break;
                    }
                }
                Ok(WsMessage::Close(_)) | Err(_) => break,
                _ => {}
            }
        }
    });

    let outbound_pump = tokio::spawn(async move {
        let mut lines = BufReader::new(bridge_reader);
        let mut buf = String::new();
        loop {
            buf.clear();
            match lines.read_line(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    let _ = serde_json::from_str::<Value>(buf.trim());
                    if ws_sink.send(WsMessage::Text(buf.clone())).await.is_err() {
                        break;
                    }
                }
            }
        }
        let _ = ws_sink.close().await;
    });

    let (owner, incoming) = Peer::start(peer_reader, peer_writer, false);
    let result = worker.run_peer(owner, incoming).await;
    inbound_pump.abort();
    outbound_pump.abort();
    result
}
