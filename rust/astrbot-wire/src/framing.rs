//! Bounded UTF-8 JSON Lines framing matching `astrbot_sdk.protocol.frames`.

use crate::{ProtocolFrame, WireError, MAX_FRAME_BYTES};
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt};

/// Read a single bounded JSONL frame (`<= MAX_FRAME_BYTES`).
/// Returns `Ok(None)` cleanly on EOF before any bytes are read.
pub async fn read_frame<R: AsyncBufRead + Unpin>(
    reader: &mut R,
) -> Result<Option<ProtocolFrame>, WireError> {
    let mut line = Vec::new();
    loop {
        let available = reader
            .fill_buf()
            .await
            .map_err(|err| WireError::host_unavailable(format!("frame read error: {err}")))?;
        if available.is_empty() {
            if line.is_empty() {
                return Ok(None);
            }
            return Err(WireError::invalid_request(
                "unexpected EOF inside protocol frame",
            ));
        }

        let newline_pos = available.iter().position(|byte| *byte == b'\n');
        let take_len = newline_pos.map_or(available.len(), |idx| idx + 1);
        if line.len() + take_len > MAX_FRAME_BYTES {
            reader.consume(take_len);
            return Err(WireError::invalid_request(
                "protocol frame exceeds 8 MiB size limit",
            ));
        }
        line.extend_from_slice(&available[..take_len]);
        reader.consume(take_len);

        if newline_pos.is_some() {
            let trimmed = line.strip_suffix(b"\n").unwrap_or(&line);
            let trimmed = trimmed.strip_suffix(b"\r").unwrap_or(trimmed);
            let frame: ProtocolFrame = serde_json::from_slice(trimmed).map_err(|err| {
                WireError::invalid_request(format!("malformed protocol frame: {err}"))
            })?;
            return Ok(Some(frame));
        }
    }
}

/// Write a single JSONL frame and flush immediately.
pub async fn write_frame<W: AsyncWrite + Unpin>(
    writer: &mut W,
    frame: &ProtocolFrame,
) -> Result<(), WireError> {
    let mut bytes = serde_json::to_vec(frame)
        .map_err(|err| WireError::invalid_request(format!("frame serialize failed: {err}")))?;
    if bytes.len() + 1 > MAX_FRAME_BYTES {
        return Err(WireError::invalid_request(
            "outbound protocol frame exceeds 8 MiB size limit",
        ));
    }
    bytes.push(b'\n');
    writer
        .write_all(&bytes)
        .await
        .map_err(|err| WireError::host_unavailable(format!("frame write error: {err}")))?;
    writer
        .flush()
        .await
        .map_err(|err| WireError::host_unavailable(format!("frame flush error: {err}")))?;
    Ok(())
}
