//! Shared instance runtime state (live config snapshot, capabilities, concurrency).

use astrbot_wire::{CapabilityGrant, WireError};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tokio::sync::Semaphore;

pub(crate) struct RuntimeState {
    pub active: AtomicBool,
    pub shutting_down: AtomicBool,
    config: Mutex<Value>,
    capabilities: Mutex<Vec<CapabilityGrant>>,
    pub capacity: Arc<Semaphore>,
}

impl RuntimeState {
    pub fn new() -> Self {
        Self {
            active: AtomicBool::new(false),
            shutting_down: AtomicBool::new(false),
            config: Mutex::new(json!({})),
            capabilities: Mutex::new(Vec::new()),
            capacity: Arc::new(Semaphore::new(64)),
        }
    }

    pub fn set_config(&self, value: Value) -> Result<(), WireError> {
        *self
            .config
            .lock()
            .map_err(|_| WireError::runtime_error("Worker config lock poisoned"))? = value;
        Ok(())
    }

    pub fn config(&self) -> Result<Value, WireError> {
        Ok(self
            .config
            .lock()
            .map_err(|_| WireError::runtime_error("Worker config lock poisoned"))?
            .clone())
    }

    pub fn set_capabilities(&self, grants: Vec<CapabilityGrant>) -> Result<(), WireError> {
        *self
            .capabilities
            .lock()
            .map_err(|_| WireError::runtime_error("Worker capability lock poisoned"))? = grants;
        Ok(())
    }

    pub fn capabilities(&self) -> Result<Vec<CapabilityGrant>, WireError> {
        Ok(self
            .capabilities
            .lock()
            .map_err(|_| WireError::runtime_error("Worker capability lock poisoned"))?
            .clone())
    }

    pub fn has_capability(&self, id: &str) -> bool {
        self.capabilities
            .lock()
            .map(|caps| caps.iter().any(|g| g.id == id))
            .unwrap_or(false)
    }

    pub fn mark_shutting_down(&self) {
        self.shutting_down.store(true, Ordering::Release);
        self.active.store(false, Ordering::Release);
    }
}
