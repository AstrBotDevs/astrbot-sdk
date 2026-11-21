//! Structured background task ownership for plugin instances.

use astrbot_wire::WireError;
use futures::FutureExt;
use std::{
    future::Future,
    panic::AssertUnwindSafe,
    sync::{Arc, Mutex},
};
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

/// Instance-scoped task group; cannot accept new tasks once shutdown begins.
#[derive(Clone, Default)]
pub struct TaskGroup(Arc<State>);

#[derive(Default)]
struct State {
    cancel: CancellationToken,
    handles: Mutex<Vec<JoinHandle<()>>>,
    error: Mutex<Option<String>>,
}

impl TaskGroup {
    pub fn is_shutdown(&self) -> bool {
        self.0.cancel.is_cancelled()
    }

    fn track(
        &self,
        name: &str,
        task: impl Future<Output = ()> + Send + 'static,
        cancel_on_shutdown: bool,
    ) -> Result<(), WireError> {
        let mut handles = self
            .0
            .handles
            .lock()
            .map_err(|_| WireError::runtime_error("plugin task lock poisoned"))?;
        if self.is_shutdown() {
            return Err(WireError::runtime_error("plugin is stopping"));
        }
        handles.retain(|handle| !handle.is_finished());
        let owner = self.clone();
        let name = name.to_owned();
        handles.push(tokio::spawn(async move {
            let work = AssertUnwindSafe(task).catch_unwind();
            let result = if cancel_on_shutdown {
                tokio::select! {
                    _ = owner.0.cancel.cancelled() => return,
                    result = work => result,
                }
            } else {
                work.await
            };
            if result.is_err() {
                tracing::error!(task = %name, "plugin background task panicked");
                if let Ok(mut error) = owner.0.error.lock() {
                    *error = Some(format!("task {name} panicked"));
                }
            }
        }));
        Ok(())
    }

    /// Spawn background work cancelled automatically when the plugin shuts down.
    pub fn spawn(&self, name: impl AsRef<str>, task: impl Future<Output = ()> + Send + 'static) {
        if let Err(err) = self.track(name.as_ref(), task, true) {
            tracing::error!(%err, task = name.as_ref(), "plugin background task rejected");
        }
    }

    /// Run a local commit/cleanup future to completion even if caller cancels, awaiting it on shutdown.
    pub async fn run_to_completion<T: Send + 'static>(
        &self,
        name: &str,
        task: impl Future<Output = T> + Send + 'static,
    ) -> Option<T> {
        let (tx, rx) = tokio::sync::oneshot::channel();
        if let Err(err) = self.track(
            name,
            async move {
                let _ = tx.send(task.await);
            },
            false,
        ) {
            tracing::error!(%err, task = name, "plugin write task rejected");
            return None;
        }
        rx.await.ok()
    }

    pub fn health(&self) -> Result<(), WireError> {
        let error = self
            .0
            .error
            .lock()
            .map_err(|_| WireError::runtime_error("plugin health lock poisoned"))?;
        match error.as_ref() {
            Some(err) => Err(WireError::runtime_error(err.clone())),
            None => Ok(()),
        }
    }

    /// Cancel cancellable background tasks and await all tracked tasks.
    pub async fn shutdown(&self) -> Result<(), WireError> {
        let handles = {
            let mut handles = self
                .0
                .handles
                .lock()
                .map_err(|_| WireError::runtime_error("plugin task lock poisoned"))?;
            self.0.cancel.cancel();
            std::mem::take(&mut *handles)
        };
        for handle in handles {
            handle.await.map_err(|err| {
                WireError::runtime_error(format!("plugin task shutdown failed: {err}"))
            })?;
        }
        self.health()
    }
}
