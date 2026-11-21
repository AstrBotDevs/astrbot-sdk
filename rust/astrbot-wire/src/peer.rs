//! Bidirectional request correlation, yield/ack flow control, cancellation,
//! and reconnectable stream recovery over bounded JSONL frames.

use crate::{ProtocolFrame, RequestId, WireError, METHOD_RESUMABLE_INVOKE, METHOD_STREAM_CONSUMED};
use serde::Serialize;
use serde_json::{Map, Value};
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    sync::{mpsc, oneshot, watch, Notify},
    task::JoinHandle,
};
use tokio_util::sync::CancellationToken;

const MAX_TOMBSTONES: usize = 1024;
const OUTBOUND_QUEUE: usize = 256;
const INBOUND_QUEUE: usize = 256;

#[derive(Debug, Clone)]
pub enum StreamItem {
    Yield { sequence: u64, result: Value },
    Completed(Value),
}

pub(crate) struct Outbound {
    pub(crate) frame: ProtocolFrame,
    pub(crate) flushed: Option<oneshot::Sender<Result<(), WireError>>>,
}

pub(crate) enum OutboundReply {
    Unary(oneshot::Sender<Result<Value, WireError>>),
    Stream(mpsc::Sender<Result<StreamItem, WireError>>),
}

pub(crate) struct OutboundCall {
    pub(crate) reply: Option<OutboundReply>,
    pub(crate) cancelled: CancellationToken,
    pub(crate) resumable: bool,
    pub(crate) last_seen_seq: u64,
    pub(crate) replay: Option<ProtocolFrame>,
}

pub(crate) struct InboundCall {
    pub(crate) cancelled: CancellationToken,
    pub(crate) resumable: bool,
    pub(crate) last_acked_seq: u64,
    pub(crate) unacked_yield: Option<ProtocolFrame>,
    pub(crate) terminal: Option<ProtocolFrame>,
    pub(crate) ack_notify: Arc<Notify>,
    pub(crate) seen_duplicate: bool,
}

#[derive(Clone)]
pub(crate) struct Terminal {
    pub(crate) id: RequestId,
    pub(crate) ended: bool,
}

pub(crate) struct Calls {
    pub(crate) outbound: BTreeMap<RequestId, OutboundCall>,
    pub(crate) inbound: BTreeMap<RequestId, InboundCall>,
    pub(crate) tombstones: VecDeque<Terminal>,
}

impl Calls {
    fn new() -> Self {
        Self {
            outbound: BTreeMap::new(),
            inbound: BTreeMap::new(),
            tombstones: VecDeque::new(),
        }
    }
}

pub(crate) struct Shared {
    prefix: &'static str,
    next_id: AtomicU64,
    calls: Mutex<Calls>,
    pub(crate) closed: CancellationToken,
    pub(crate) draining: AtomicBool,
    pub(crate) connected: watch::Sender<bool>,
    reconnect_tx: Mutex<Option<mpsc::Sender<crate::reconnect::Transport>>>,
    outbound_tx: mpsc::Sender<Outbound>,
}

impl Shared {
    pub(crate) fn calls(&self) -> Result<std::sync::MutexGuard<'_, Calls>, WireError> {
        self.calls
            .lock()
            .map_err(|_| WireError::runtime_error("peer call table poisoned"))
    }

    pub(crate) fn close(&self) {
        self.closed.cancel();
        if let Ok(mut calls) = self.calls.lock() {
            for (_, mut call) in std::mem::take(&mut calls.outbound) {
                call.cancelled.cancel();
                if let Some(OutboundReply::Unary(tx)) = call.reply.take() {
                    let _ = tx.send(Err(closed_error()));
                } else if let Some(OutboundReply::Stream(tx)) = call.reply.take() {
                    let _ = tx.try_send(Err(closed_error()));
                }
            }
            for (_, call) in std::mem::take(&mut calls.inbound) {
                call.cancelled.cancel();
                call.ack_notify.notify_waiters();
            }
        }
    }
}

pub(crate) fn closed_error() -> WireError {
    WireError::host_unavailable("Worker peer closed")
}

pub(crate) fn record_tombstone(calls: &mut Calls, id: RequestId, ended: bool) {
    calls.tombstones.push_back(Terminal { id, ended });
    while calls.tombstones.len() > MAX_TOMBSTONES {
        calls.tombstones.pop_front();
    }
}

/// Clonable handle to a framed bidirectional protocol peer.
#[derive(Clone)]
pub struct Peer {
    pub(crate) shared: Arc<Shared>,
}

/// Incoming request received from the remote peer.
pub struct Incoming {
    pub id: RequestId,
    pub method: String,
    pub params: Map<String, Value>,
    pub cancelled: CancellationToken,
    pub resumable: bool,
    peer: Peer,
    finished: bool,
}

impl Incoming {
    /// Emit a single flow-controlled yield frame.
    /// For resumable calls, waits until the previous unacked yield is consumed.
    pub async fn send_yield(
        &self,
        sequence: u64,
        result: Value,
        wait_for_ack: bool,
    ) -> Result<(), WireError> {
        let frame = ProtocolFrame::Yield {
            id: self.id.clone(),
            sequence,
            result,
        };
        let ack_notify = loop {
            let wait_notify = {
                let mut calls = self.peer.shared.calls()?;
                let call = calls
                    .inbound
                    .get_mut(&self.id)
                    .ok_or_else(|| WireError::cancelled("invocation already ended"))?;
                if call.cancelled.is_cancelled() {
                    return Err(WireError::cancelled("invocation cancelled"));
                }
                if call.resumable && call.unacked_yield.is_some() {
                    Some(call.ack_notify.clone())
                } else {
                    if call.resumable {
                        call.unacked_yield = Some(frame.clone());
                    }
                    break call.ack_notify.clone();
                }
            };
            if let Some(notify) = wait_notify {
                tokio::select! {
                    _ = self.cancelled.cancelled() => return Err(WireError::cancelled("invocation cancelled")),
                    _ = self.peer.shared.closed.cancelled() => return Err(closed_error()),
                    _ = notify.notified() => {}
                }
            }
        };

        self.peer.enqueue(frame, false).await?;

        if !wait_for_ack {
            let mut calls = self.peer.shared.calls()?;
            if let Some(call) = calls.inbound.get_mut(&self.id) {
                call.unacked_yield = None;
                call.last_acked_seq = call.last_acked_seq.max(sequence);
            }
            return Ok(());
        }

        loop {
            {
                let calls = self.peer.shared.calls()?;
                let Some(call) = calls.inbound.get(&self.id) else {
                    return Err(WireError::cancelled("invocation ended while awaiting ack"));
                };
                if call.last_acked_seq >= sequence {
                    return Ok(());
                }
            }
            tokio::select! {
                _ = self.cancelled.cancelled() => return Err(WireError::cancelled("invocation cancelled")),
                _ = self.peer.shared.closed.cancelled() => return Err(closed_error()),
                _ = ack_notify.notified() => {}
            }
        }
    }

    /// Complete the inbound call with a final response or error frame.
    /// Waits for the frame to flush to the transport.
    pub async fn respond(mut self, outcome: Result<Value, WireError>) -> Result<(), WireError> {
        self.finished = true;
        let frame = match outcome {
            Ok(result) => ProtocolFrame::Response {
                id: self.id.clone(),
                result,
            },
            Err(err) => ProtocolFrame::Error {
                id: self.id.clone(),
                code: err.code,
                message: err.message,
                data: err.data,
            },
        };
        {
            let mut calls = self.peer.shared.calls()?;
            if let Some(call) = calls.inbound.get_mut(&self.id) {
                if call.resumable {
                    call.terminal = Some(frame.clone());
                } else {
                    calls.inbound.remove(&self.id);
                    record_tombstone(&mut calls, self.id.clone(), true);
                }
            }
        }
        let result = self.peer.enqueue(frame, true).await;
        if result.is_ok() {
            if let Ok(mut calls) = self.peer.shared.calls() {
                if calls.inbound.remove(&self.id).is_some() {
                    record_tombstone(&mut calls, self.id.clone(), true);
                }
            }
        }
        result
    }
}

impl Drop for Incoming {
    fn drop(&mut self) {
        if !self.finished {
            if let Ok(mut calls) = self.peer.shared.calls() {
                if calls.inbound.remove(&self.id).is_some() {
                    record_tombstone(&mut calls, self.id.clone(), false);
                }
            }
        }
    }
}

/// Handle for reading flow-controlled stream items from an outbound request.
pub struct StreamHandle {
    pub id: RequestId,
    rx: mpsc::Receiver<Result<StreamItem, WireError>>,
    peer: Peer,
    completed: bool,
}

impl StreamHandle {
    pub async fn next(&mut self) -> Option<Result<StreamItem, WireError>> {
        let item = self.rx.recv().await?;
        if matches!(item, Ok(StreamItem::Completed(_)) | Err(_)) {
            self.completed = true;
        }
        Some(item)
    }

    pub async fn ack(&self, sequence: u64) -> Result<(), WireError> {
        self.peer
            .enqueue(
                ProtocolFrame::Ack {
                    id: self.id.clone(),
                    sequence,
                },
                false,
            )
            .await
    }
}

impl Drop for StreamHandle {
    fn drop(&mut self) {
        if !self.completed {
            self.peer.cancel_outbound(&self.id);
        }
    }
}

/// Owns the background peer I/O task. Calling `shutdown` drains terminal responses cleanly.
pub struct PeerOwner {
    peer: Peer,
    task: Option<JoinHandle<()>>,
}

impl PeerOwner {
    pub fn peer(&self) -> Peer {
        self.peer.clone()
    }

    /// Stop accepting new work and wait for queued terminal replies to flush.
    pub async fn shutdown(mut self) {
        self.peer.shared.draining.store(true, Ordering::Release);
        self.peer.shared.close();
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}

impl Drop for PeerOwner {
    fn drop(&mut self) {
        self.peer.shared.close();
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

impl Peer {
    /// Start a standard non-reconnectable peer on `reader` and `writer`.
    pub fn start<R, W>(reader: R, writer: W, is_host: bool) -> (PeerOwner, mpsc::Receiver<Incoming>)
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        Self::start_with_mode(reader, writer, is_host, None)
    }

    /// Start a reconnectable peer keeping resumable invocations alive during `grace`.
    pub fn start_reconnectable<R, W>(
        reader: R,
        writer: W,
        is_host: bool,
        grace: Duration,
    ) -> (PeerOwner, mpsc::Receiver<Incoming>)
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        Self::start_with_mode(reader, writer, is_host, Some(grace))
    }

    fn start_with_mode<R, W>(
        reader: R,
        writer: W,
        is_host: bool,
        grace: Option<Duration>,
    ) -> (PeerOwner, mpsc::Receiver<Incoming>)
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let (outbound_tx, outbound_rx) = mpsc::channel(OUTBOUND_QUEUE);
        let (incoming_tx, incoming_rx) = mpsc::channel(INBOUND_QUEUE);
        let (reconnect_tx, reconnect_rx) = mpsc::channel(4);
        let (connected_tx, _) = watch::channel(true);

        let shared = Arc::new(Shared {
            prefix: if is_host { "host:" } else { "worker:" },
            next_id: AtomicU64::new(1),
            calls: Mutex::new(Calls::new()),
            closed: CancellationToken::new(),
            draining: AtomicBool::new(false),
            connected: connected_tx,
            reconnect_tx: Mutex::new(Some(reconnect_tx)),
            outbound_tx,
        });
        let peer = Self { shared };
        let transport = crate::reconnect::Transport::new(reader, writer);
        let task = tokio::spawn(crate::reconnect::run(
            peer.clone(),
            transport,
            outbound_rx,
            incoming_tx,
            reconnect_rx,
            grace,
        ));
        (
            PeerOwner {
                peer,
                task: Some(task),
            },
            incoming_rx,
        )
    }

    pub fn is_closed(&self) -> bool {
        self.shared.closed.is_cancelled()
    }

    pub async fn wait_disconnected(&self) {
        let mut rx = self.shared.connected.subscribe();
        while *rx.borrow() && !self.is_closed() {
            if rx.changed().await.is_err() {
                break;
            }
        }
    }

    pub async fn wait_closed(&self) {
        self.shared.closed.cancelled().await;
    }

    pub fn close(&self) {
        self.shared.close();
    }

    /// Attach a fresh physical transport to a reconnectable peer.
    pub async fn reconnect<R, W>(&self, reader: R, writer: W) -> Result<(), WireError>
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        if self.is_closed() {
            return Err(closed_error());
        }
        let sender = self
            .shared
            .reconnect_tx
            .lock()
            .map_err(|_| WireError::runtime_error("reconnect lock poisoned"))?
            .clone()
            .ok_or_else(closed_error)?;
        let (tx, rx) = oneshot::channel();
        let mut transport = crate::reconnect::Transport::new(reader, writer);
        transport.accepted = Some(tx);
        sender.send(transport).await.map_err(|_| closed_error())?;
        rx.await.map_err(|_| closed_error())?
    }

    pub(crate) async fn enqueue(
        &self,
        frame: ProtocolFrame,
        wait_flush: bool,
    ) -> Result<(), WireError> {
        if self.shared.closed.is_cancelled() && !self.shared.draining.load(Ordering::Acquire) {
            return Err(closed_error());
        }
        let (flushed_tx, flushed_rx) = if wait_flush {
            let (tx, rx) = oneshot::channel();
            (Some(tx), Some(rx))
        } else {
            (None, None)
        };
        self.shared
            .outbound_tx
            .send(Outbound {
                frame,
                flushed: flushed_tx,
            })
            .await
            .map_err(|_| closed_error())?;
        if let Some(rx) = flushed_rx {
            rx.await.map_err(|_| closed_error())??;
        }
        Ok(())
    }

    pub fn next_request_id(&self) -> RequestId {
        let seq = self.shared.next_id.fetch_add(1, Ordering::Relaxed);
        RequestId::new(format!("{}{}", self.shared.prefix, seq))
            .expect("generated request ID is always within length limit")
    }

    /// Send a unary RPC request and await its response, propagating cancellation and timeout.
    pub async fn request<P: Serialize>(
        &self,
        method: &str,
        params: &P,
        timeout: Duration,
        cancellation: &CancellationToken,
    ) -> Result<Value, WireError> {
        self.request_inner(method, params, timeout, cancellation, false)
            .await
    }

    /// Send a unary RPC request, ensuring it only returns a delivery-confirmed result or error.
    pub async fn request_with_delivery<P: Serialize>(
        &self,
        method: &str,
        params: &P,
        timeout: Duration,
        cancellation: &CancellationToken,
    ) -> Result<Value, WireError> {
        self.request_inner(method, params, timeout, cancellation, true)
            .await
    }

    async fn request_inner<P: Serialize>(
        &self,
        method: &str,
        params: &P,
        timeout: Duration,
        cancellation: &CancellationToken,
        wait_flush: bool,
    ) -> Result<Value, WireError> {
        if self.is_closed() {
            return Err(closed_error());
        }
        if cancellation.is_cancelled() {
            return Err(WireError::cancelled("request cancelled before send"));
        }
        let params_val = serde_json::to_value(params)
            .map_err(|err| WireError::invalid_request(err.to_string()))?;
        let params_map = params_val
            .as_object()
            .cloned()
            .ok_or_else(|| WireError::invalid_request("request params must be a JSON object"))?;

        let id = self.next_request_id();
        let frame = ProtocolFrame::Request {
            id: id.clone(),
            method: method.to_owned(),
            params: params_map,
        };
        let (tx, rx) = oneshot::channel();
        let call_cancel = CancellationToken::new();
        {
            let mut calls = self.shared.calls()?;
            calls.outbound.insert(
                id.clone(),
                OutboundCall {
                    reply: Some(OutboundReply::Unary(tx)),
                    cancelled: call_cancel.clone(),
                    resumable: false,
                    last_seen_seq: 0,
                    replay: None,
                },
            );
        }

        if let Err(err) = self.enqueue(frame, wait_flush).await {
            if let Ok(mut calls) = self.shared.calls() {
                calls.outbound.remove(&id);
            }
            return Err(err);
        }

        let outcome = tokio::select! {
            biased;
            res = tokio::time::timeout(timeout, rx) => {
                match res {
                    Ok(Ok(val)) => val,
                    Ok(Err(_)) => Err(closed_error()),
                    Err(_) => {
                        self.cancel_outbound(&id);
                        Err(WireError::new("DEADLINE_EXCEEDED", "request timed out"))
                    }
                }
            }
            _ = cancellation.cancelled() => {
                self.cancel_outbound(&id);
                Err(WireError::cancelled("request cancelled"))
            }
        };
        if let Ok(mut calls) = self.shared.calls() {
            calls.outbound.remove(&id);
        }
        outcome
    }

    /// Send a streaming RPC request receiving `YieldFrame`s until `ResponseFrame` or `ErrorFrame`.
    pub async fn request_stream<P: Serialize>(
        &self,
        method: &str,
        params: &P,
    ) -> Result<StreamHandle, WireError> {
        if self.is_closed() {
            return Err(closed_error());
        }
        let params_val = serde_json::to_value(params)
            .map_err(|err| WireError::invalid_request(err.to_string()))?;
        let params_map = params_val
            .as_object()
            .cloned()
            .ok_or_else(|| WireError::invalid_request("request params must be a JSON object"))?;

        let id = self.next_request_id();
        let frame = ProtocolFrame::Request {
            id: id.clone(),
            method: method.to_owned(),
            params: params_map,
        };
        let (tx, rx) = mpsc::channel(64);
        let resumable = method == METHOD_RESUMABLE_INVOKE;
        {
            let mut calls = self.shared.calls()?;
            calls.outbound.insert(
                id.clone(),
                OutboundCall {
                    reply: Some(OutboundReply::Stream(tx)),
                    cancelled: CancellationToken::new(),
                    resumable,
                    last_seen_seq: 0,
                    replay: None,
                },
            );
        }
        if let Err(err) = self.enqueue(frame, false).await {
            if let Ok(mut calls) = self.shared.calls() {
                calls.outbound.remove(&id);
            }
            return Err(err);
        }
        Ok(StreamHandle {
            id,
            rx,
            peer: self.clone(),
            completed: false,
        })
    }

    fn cancel_outbound(&self, id: &RequestId) {
        if let Ok(mut calls) = self.shared.calls() {
            if let Some(mut call) = calls.outbound.remove(id) {
                call.cancelled.cancel();
                drop(call.reply.take());
                record_tombstone(&mut calls, id.clone(), false);
            }
        }
        let tx = self.shared.outbound_tx.clone();
        let id = id.clone();
        let _ = tx.try_send(Outbound {
            frame: ProtocolFrame::Cancel { id },
            flushed: None,
        });
    }

    /// Dispatch one inbound protocol frame received from the transport.
    pub(crate) async fn handle_frame(
        &self,
        frame: ProtocolFrame,
        incoming_tx: &mpsc::Sender<Incoming>,
    ) -> Result<(), WireError> {
        match frame {
            ProtocolFrame::Request { id, method, params } => {
                if method == METHOD_STREAM_CONSUMED {
                    if let Some(seq) = params.get("sequence").and_then(Value::as_u64) {
                        let target = params
                            .get("id")
                            .and_then(Value::as_str)
                            .and_then(|s| RequestId::new(s).ok());
                        if let Some(target_id) = target {
                            self.ack_inbound(&target_id, seq);
                        }
                    }
                    return Ok(());
                }
                let resumable = method == METHOD_RESUMABLE_INVOKE;
                let cancel = CancellationToken::new();
                {
                    let mut calls = self.shared.calls()?;
                    if let Some(existing) = calls.inbound.get_mut(&id) {
                        if resumable {
                            existing.seen_duplicate = true;
                            return Ok(());
                        }
                        return Err(WireError::invalid_request(format!(
                            "duplicate request id: {}",
                            id.as_str()
                        )));
                    }
                    if calls.tombstones.iter().any(|t| t.id == id) {
                        if resumable {
                            return Ok(());
                        }
                        return Err(WireError::invalid_request(format!(
                            "duplicate completed request id: {}",
                            id.as_str()
                        )));
                    }
                    calls.inbound.insert(
                        id.clone(),
                        InboundCall {
                            cancelled: cancel.clone(),
                            resumable,
                            last_acked_seq: 0,
                            unacked_yield: None,
                            terminal: None,
                            ack_notify: Arc::new(Notify::new()),
                            seen_duplicate: false,
                        },
                    );
                }
                let incoming = Incoming {
                    id,
                    method,
                    params,
                    cancelled: cancel,
                    resumable,
                    peer: self.clone(),
                    finished: false,
                };
                if incoming_tx.send(incoming).await.is_err() {
                    return Err(closed_error());
                }
            }
            ProtocolFrame::Response { id, result } => {
                let mut calls = self.shared.calls()?;
                if let Some(mut call) = calls.outbound.remove(&id) {
                    record_tombstone(&mut calls, id, true);
                    match call.reply.take() {
                        Some(OutboundReply::Unary(tx)) => {
                            let _ = tx.send(Ok(result));
                        }
                        Some(OutboundReply::Stream(tx)) => {
                            let _ = tx.try_send(Ok(StreamItem::Completed(result)));
                        }
                        None => {}
                    }
                }
            }
            ProtocolFrame::Error {
                id,
                code,
                message,
                data,
            } => {
                let mut calls = self.shared.calls()?;
                if let Some(mut call) = calls.outbound.remove(&id) {
                    record_tombstone(&mut calls, id, true);
                    let err = WireError {
                        code,
                        message,
                        data,
                    };
                    match call.reply.take() {
                        Some(OutboundReply::Unary(tx)) => {
                            let _ = tx.send(Err(err));
                        }
                        Some(OutboundReply::Stream(tx)) => {
                            let _ = tx.try_send(Err(err));
                        }
                        None => {}
                    }
                }
            }
            ProtocolFrame::Yield {
                id,
                sequence,
                result,
            } => {
                let (tx, re_ack) = {
                    let mut calls = self.shared.calls()?;
                    match calls.outbound.get_mut(&id) {
                        Some(c) if sequence <= c.last_seen_seq => (None, c.resumable),
                        Some(c) => {
                            c.last_seen_seq = sequence;
                            let tx = match &c.reply {
                                Some(OutboundReply::Stream(tx)) => Some(tx.clone()),
                                _ => None,
                            };
                            (tx, false)
                        }
                        None => (None, false),
                    }
                };
                if re_ack {
                    let _ = self
                        .enqueue(ProtocolFrame::Ack { id, sequence }, false)
                        .await;
                } else if let Some(tx) = tx {
                    let _ = tx.send(Ok(StreamItem::Yield { sequence, result })).await;
                }
            }
            ProtocolFrame::Ack { id, sequence } => {
                self.ack_inbound(&id, sequence);
            }
            ProtocolFrame::Cancel { id } => {
                let mut calls = self.shared.calls()?;
                if let Some(call) = calls.inbound.get(&id) {
                    call.cancelled.cancel();
                    call.ack_notify.notify_waiters();
                } else if let Some(mut call) = calls.outbound.remove(&id) {
                    call.cancelled.cancel();
                    if let Some(OutboundReply::Unary(tx)) = call.reply.take() {
                        let _ = tx.send(Err(WireError::cancelled("cancelled by remote peer")));
                    }
                }
            }
        }
        Ok(())
    }

    fn ack_inbound(&self, id: &RequestId, sequence: u64) {
        if let Ok(mut calls) = self.shared.calls() {
            if let Some(call) = calls.inbound.get_mut(id) {
                if sequence > call.last_acked_seq {
                    call.last_acked_seq = sequence;
                    if let Some(ProtocolFrame::Yield {
                        sequence: unacked, ..
                    }) = &call.unacked_yield
                    {
                        if *unacked <= sequence {
                            call.unacked_yield = None;
                        }
                    }
                    call.ack_notify.notify_waiters();
                }
            }
        }
    }
}
