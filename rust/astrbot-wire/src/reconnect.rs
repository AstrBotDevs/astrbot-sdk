//! Transport ownership and 30-second resumable stream recovery (`resume_stream_v1`).

use crate::{
    framing,
    peer::{closed_error, record_tombstone, Calls, Incoming, Outbound, Peer, Shared},
    ProtocolFrame, WireError,
};
use std::{collections::VecDeque, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncBufRead, AsyncRead, AsyncWrite, BufReader, BufWriter},
    sync::{mpsc, oneshot},
};

pub const DEFAULT_RESUME_GRACE: Duration = Duration::from_secs(30);

pub(crate) struct Transport {
    reader: Box<dyn AsyncBufRead + Unpin + Send>,
    writer: Box<dyn AsyncWrite + Unpin + Send>,
    pub(crate) accepted: Option<oneshot::Sender<Result<(), WireError>>>,
}

impl Transport {
    pub(crate) fn new<R, W>(reader: R, writer: W) -> Self
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        Self {
            reader: Box::new(BufReader::new(reader)),
            writer: Box::new(BufWriter::new(writer)),
            accepted: None,
        }
    }
}

pub(crate) async fn run(
    peer: Peer,
    initial_transport: Transport,
    mut outbound: mpsc::Receiver<Outbound>,
    incoming: mpsc::Sender<Incoming>,
    mut reconnect: mpsc::Receiver<Transport>,
    grace: Option<Duration>,
) {
    let mut transport = Some(initial_transport);
    let mut replay: VecDeque<ProtocolFrame> = VecDeque::new();
    while let Some(active) = transport.as_mut() {
        let outcome = tokio::select! {
            biased;
            _ = peer.shared.closed.cancelled() => break,
            result = read_loop(&mut active.reader, &peer, &incoming) => result,
            result = write_loop(&mut active.writer, &peer.shared, &mut outbound, &mut replay) => result,
        };

        let recoverable = outcome
            .as_ref()
            .is_err_and(|err| err.code == "HOST_UNAVAILABLE");
        if let Err(err) = &outcome {
            tracing::debug!(%err, "Worker transport stopped");
        }

        let Some(grace_duration) = grace.filter(|_| recoverable && !peer.is_closed()) else {
            break;
        };

        peer.shared.connected.send_replace(false);
        if let Err(err) = suspend_non_resumable(&peer.shared) {
            tracing::warn!(%err, "Worker calls could not be suspended");
            break;
        }
        while let Ok(queued) = outbound.try_recv() {
            if let Some(flushed) = queued.flushed {
                let _ = flushed.send(Err(closed_error()));
            }
        }
        transport = None;

        let next = tokio::select! {
            biased;
            _ = peer.shared.closed.cancelled() => None,
            result = tokio::time::timeout(grace_duration, reconnect.recv()) => result.ok().flatten(),
        };
        let Some(mut next) = next else {
            break;
        };

        match build_replay(&peer.shared) {
            Ok(frames) => replay = frames,
            Err(err) => {
                tracing::warn!(%err, "Worker calls could not be resumed");
                break;
            }
        }
        peer.shared.connected.send_replace(true);
        if let Some(accepted) = next.accepted.take() {
            let _ = accepted.send(Ok(()));
        }
        transport = Some(next);
    }

    // Flush any already-queued frames if the transport is still open before tearing down.
    while let Ok(queued) = outbound.try_recv() {
        let res = match transport.as_mut() {
            Some(active) => framing::write_frame(&mut active.writer, &queued.frame).await,
            None => Err(closed_error()),
        };
        if let Some(flushed) = queued.flushed {
            let _ = flushed.send(res);
        }
    }
    peer.shared.connected.send_replace(false);
    peer.shared.close();
}

async fn read_loop<R: tokio::io::AsyncBufRead + Unpin>(
    reader: &mut R,
    peer: &Peer,
    incoming: &mpsc::Sender<Incoming>,
) -> Result<(), WireError> {
    loop {
        let Some(frame) = framing::read_frame(reader).await? else {
            return Err(closed_error());
        };
        peer.handle_frame(frame, incoming).await?;
    }
}

async fn write_loop<W: AsyncWrite + Unpin>(
    writer: &mut W,
    _shared: &Arc<Shared>,
    outbound: &mut mpsc::Receiver<Outbound>,
    replay: &mut VecDeque<ProtocolFrame>,
) -> Result<(), WireError> {
    while let Some(frame) = replay.pop_front() {
        framing::write_frame(writer, &frame).await?;
    }
    while let Some(item) = outbound.recv().await {
        let res = framing::write_frame(writer, &item.frame).await;
        if let Some(flushed) = item.flushed {
            let _ = flushed.send(res.clone());
        }
        res?;
    }
    Err(closed_error())
}

fn suspend_non_resumable(shared: &Shared) -> Result<(), WireError> {
    let mut calls = shared.calls()?;
    let ordinary_ids: Vec<_> = calls
        .outbound
        .iter()
        .filter(|(_, call)| !call.resumable && call.replay.is_none())
        .map(|(id, _)| id.clone())
        .collect();
    for id in ordinary_ids {
        if let Some(mut call) = calls.outbound.remove(&id) {
            call.cancelled.cancel();
            drop(call.reply.take());
            record_tombstone(&mut calls, id, false);
        }
    }
    for call in calls.inbound.values() {
        if !call.resumable {
            call.cancelled.cancel();
            call.ack_notify.notify_waiters();
        }
    }
    Ok(())
}

fn build_replay(shared: &Shared) -> Result<VecDeque<ProtocolFrame>, WireError> {
    let calls: std::sync::MutexGuard<'_, Calls> = shared.calls()?;
    let mut replay = VecDeque::new();
    for terminal in &calls.tombstones {
        if !terminal.ended {
            replay.push_back(ProtocolFrame::Cancel {
                id: terminal.id.clone(),
            });
        }
    }
    for call in calls.outbound.values() {
        if !call.cancelled.is_cancelled() {
            if let Some(frame) = &call.replay {
                replay.push_back(frame.clone());
            }
        }
    }
    for call in calls.inbound.values() {
        if let Some(unacked) = &call.unacked_yield {
            replay.push_back(unacked.clone());
        }
        if let Some(terminal) = &call.terminal {
            replay.push_back(terminal.clone());
        }
    }
    Ok(replay)
}
