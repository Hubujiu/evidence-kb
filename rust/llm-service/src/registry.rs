use crate::LlmError;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{
    Arc, Mutex, MutexGuard,
    atomic::{AtomicU64, Ordering},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Binding {
    pub service: u64,
    pub registration: u64,
    pub generation: u64,
}

impl Binding {
    pub fn replay_identity(&self) -> String {
        format!("{}:{}:{}", self.service, self.registration, self.generation)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RegistrationIdentity {
    pub registration_id: Value,
    pub owner: Option<Value>,
}

impl RegistrationIdentity {
    fn validate(&self) -> Result<String, LlmError> {
        if !self
            .registration_id
            .as_str()
            .is_some_and(|id| !id.is_empty())
            && !self.registration_id.as_u64().is_some_and(|id| id > 0)
        {
            return Err(LlmError::new(
                "INVALID_LLM_REGISTRATION_ID",
                "registration id must be nonempty text or a positive integer",
            ));
        }
        if self.owner.as_ref().is_some_and(|owner| !owner.is_object()) {
            return Err(LlmError::new(
                "INVALID_LLM_OWNER",
                "owner identity must be an object",
            ));
        }
        serde_json::to_string(self)
            .map_err(|error| LlmError::new("INVALID_LLM_REGISTRATION_ID", error.to_string()))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RoutePhase {
    Active,
    Draining,
    Cleaning,
    CleanupFailed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ServicePhase {
    Active,
    Draining,
    Closed,
}

pub struct RouteSnapshot<T: ?Sized> {
    pub name: String,
    pub binding: Binding,
    pub identity: RegistrationIdentity,
    pub phase: RoutePhase,
    pub active: usize,
    pub cleanup_error: Option<String>,
    pub payload: Arc<T>,
}

struct Entry<T: ?Sized> {
    name: String,
    generation: u64,
    identity: RegistrationIdentity,
    phase: RoutePhase,
    active: usize,
    cleanup_error: Option<String>,
    detached: bool,
    payload: Arc<T>,
}

struct State<T: ?Sized> {
    next: u64,
    phase: ServicePhase,
    routes: BTreeMap<String, u64>,
    entries: BTreeMap<u64, Entry<T>>,
    issued: BTreeSet<String>,
}

struct Inner<T: ?Sized> {
    identity: u64,
    state: Mutex<State<T>>,
}

pub struct Registry<T: ?Sized> {
    inner: Arc<Inner<T>>,
}

impl<T: ?Sized> Default for Registry<T> {
    fn default() -> Self {
        static IDENTITIES: AtomicU64 = AtomicU64::new(1);
        Self {
            inner: Arc::new(Inner {
                identity: IDENTITIES.fetch_add(1, Ordering::SeqCst),
                state: Mutex::new(State {
                    next: 0,
                    phase: ServicePhase::Active,
                    routes: BTreeMap::new(),
                    entries: BTreeMap::new(),
                    issued: BTreeSet::new(),
                }),
            }),
        }
    }
}

impl<T: ?Sized> Inner<T> {
    fn lock(&self) -> Result<MutexGuard<'_, State<T>>, LlmError> {
        self.state
            .lock()
            .map_err(|_| LlmError::new("LLM_SERVICE_UNAVAILABLE", "registry lock poisoned"))
    }

    fn snapshot(&self, registration: u64, entry: &Entry<T>) -> RouteSnapshot<T> {
        RouteSnapshot {
            name: entry.name.clone(),
            binding: Binding {
                service: self.identity,
                registration,
                generation: entry.generation,
            },
            identity: entry.identity.clone(),
            phase: entry.phase,
            active: entry.active,
            cleanup_error: entry.cleanup_error.clone(),
            payload: entry.payload.clone(),
        }
    }

    fn current<'a>(&self, state: &'a State<T>, binding: Binding) -> Result<&'a Entry<T>, LlmError> {
        let entry = state.entries.get(&binding.registration).filter(|entry| {
            binding.service == self.identity
                && binding.generation == entry.generation
                && state.routes.get(&entry.name) == Some(&binding.registration)
                && entry.phase == RoutePhase::Active
                && state.phase == ServicePhase::Active
        });
        entry.ok_or_else(|| {
            LlmError::new(
                "LLM_BINDING_CHANGED",
                "prepared registration is no longer active",
            )
        })
    }
}

impl<T: ?Sized> Registry<T> {
    pub fn identity(&self) -> u64 {
        self.inner.identity
    }

    pub fn register(
        &self,
        name: &str,
        payload: Arc<T>,
        identity: Option<RegistrationIdentity>,
    ) -> Result<Binding, LlmError> {
        if name.is_empty() {
            return Err(LlmError::new("INVALID_LLM_PROVIDER", "empty route name"));
        }
        let mut state = self.inner.lock()?;
        if state.phase != ServicePhase::Active || state.routes.contains_key(name) {
            return Err(LlmError::new(
                "LLM_PROVIDER_ALREADY_REGISTERED",
                "route cannot be registered",
            ));
        }
        let generation = state
            .next
            .checked_add(1)
            .ok_or_else(|| LlmError::new("LLM_GENERATION_EXHAUSTED", "generation exhausted"))?;
        let mut identity = identity.unwrap_or_else(|| RegistrationIdentity {
            registration_id: Value::String(format!("{}:{generation}", self.identity())),
            owner: None,
        });
        if identity.registration_id.is_null() {
            identity.registration_id = Value::String(format!("{}:{generation}", self.identity()));
        }
        let key = identity.validate()?;
        if state.issued.contains(&key) {
            return Err(LlmError::new(
                "LLM_REGISTRATION_ID_REUSED",
                "registration identity was already issued",
            ));
        }
        state.next = generation;
        state.issued.insert(key);
        state.routes.insert(name.into(), generation);
        state.entries.insert(
            generation,
            Entry {
                name: name.into(),
                generation,
                identity,
                phase: RoutePhase::Active,
                active: 0,
                cleanup_error: None,
                detached: false,
                payload,
            },
        );
        Ok(Binding {
            service: self.identity(),
            registration: generation,
            generation,
        })
    }

    pub fn inspect(&self) -> Result<Vec<RouteSnapshot<T>>, LlmError> {
        Ok(self
            .inner
            .lock()?
            .entries
            .iter()
            .filter(|(_, entry)| !entry.detached)
            .map(|(registration, entry)| self.inner.snapshot(*registration, entry))
            .collect())
    }

    pub fn prepare(
        &self,
        name: &str,
        expected: Option<&RegistrationIdentity>,
    ) -> Result<Prepared<T>, LlmError> {
        let state = self.inner.lock()?;
        let registration = state
            .routes
            .get(name)
            .ok_or_else(|| LlmError::new("LLM_PROVIDER_UNAVAILABLE", name))?;
        let entry = &state.entries[registration];
        if entry.phase != RoutePhase::Active || state.phase != ServicePhase::Active {
            return Err(LlmError::new(
                "LLM_PROVIDER_UNAVAILABLE",
                "route is draining",
            ));
        }
        if expected.is_some_and(|identity| *identity != entry.identity) {
            return Err(LlmError::new(
                "LLM_BINDING_CHANGED",
                "route registration changed",
            ));
        }
        Ok(Prepared {
            inner: self.inner.clone(),
            snapshot: self.inner.snapshot(*registration, entry),
        })
    }

    pub fn detach(&self, name: &str, generation: u64) -> Result<(), LlmError> {
        let mut state = self.inner.lock()?;
        let registration = *state
            .routes
            .get(name)
            .ok_or_else(|| LlmError::new("LLM_PROVIDER_UNAVAILABLE", name))?;
        let entry = state
            .entries
            .get_mut(&registration)
            .expect("route has an entry");
        if entry.generation != generation {
            return Err(LlmError::new(
                "LLM_BINDING_CHANGED",
                "stale disposer cannot detach its replacement",
            ));
        }
        entry.phase = RoutePhase::Draining;
        entry.detached = true;
        let remove = entry.active == 0;
        state.routes.remove(name);
        let retired = if remove {
            state.entries.remove(&registration)
        } else {
            None
        };
        drop(state);
        drop(retired);
        Ok(())
    }

    pub fn begin_cleanup(
        &self,
        name: &str,
        binding: Binding,
    ) -> Result<Option<RouteSnapshot<T>>, LlmError> {
        let mut state = self.inner.lock()?;
        let registration = binding.registration;
        if binding.service != self.identity() || state.routes.get(name) != Some(&registration) {
            return Ok(None);
        }
        let entry = state
            .entries
            .get_mut(&registration)
            .expect("route has an entry");
        if entry.generation != binding.generation {
            return Ok(None);
        }
        if entry.phase == RoutePhase::Cleaning {
            return Err(LlmError::new(
                "LLM_CLEANUP_BUSY",
                "cleanup is already running",
            ));
        }
        entry.phase = RoutePhase::Draining;
        if entry.active != 0 {
            return Err(LlmError::new("LLM_BUSY", "route still owns active calls"));
        }
        entry.phase = RoutePhase::Cleaning;
        entry.cleanup_error = None;
        Ok(Some(self.inner.snapshot(registration, entry)))
    }

    pub fn finish_cleanup(&self, binding: Binding, error: Option<String>) -> Result<(), LlmError> {
        let mut state = self.inner.lock()?;
        let registration = binding.registration;
        let entry = state
            .entries
            .get_mut(&registration)
            .filter(|entry| {
                binding.service == self.identity() && entry.generation == binding.generation
            })
            .ok_or_else(|| LlmError::new("LLM_BINDING_CHANGED", "registration no longer exists"))?;
        if entry.phase != RoutePhase::Cleaning || entry.active != 0 {
            return Err(LlmError::new(
                "INVALID_LLM_CLEANUP",
                "route is not awaiting cleanup completion",
            ));
        }
        let retired = if let Some(error) = error {
            entry.phase = RoutePhase::CleanupFailed;
            entry.cleanup_error = Some(error);
            None
        } else {
            let name = entry.name.clone();
            state.routes.remove(&name);
            state.entries.remove(&registration)
        };
        drop(state);
        drop(retired);
        Ok(())
    }

    pub fn begin_close(&self) -> Result<Vec<RouteSnapshot<T>>, LlmError> {
        let mut state = self.inner.lock()?;
        if state.entries.values().any(|entry| entry.active != 0) {
            return Err(LlmError::new("LLM_BUSY", "service still owns active calls"));
        }
        if state.phase != ServicePhase::Closed {
            state.phase = ServicePhase::Draining;
        }
        Ok(state
            .entries
            .iter()
            .rev()
            .map(|(id, entry)| self.inner.snapshot(*id, entry))
            .collect())
    }

    pub fn finish_close(&self) -> Result<(), LlmError> {
        let mut state = self.inner.lock()?;
        if !state.entries.is_empty() || !state.routes.is_empty() {
            return Err(LlmError::new(
                "LLM_BUSY",
                "service still owns registrations",
            ));
        }
        state.phase = ServicePhase::Closed;
        state.issued.clear();
        Ok(())
    }

    pub fn adopt(
        &self,
        identities: Vec<(u64, RegistrationIdentity)>,
    ) -> Result<Vec<RouteSnapshot<T>>, LlmError> {
        let mut state = self.inner.lock()?;
        if state.phase != ServicePhase::Active
            || state
                .entries
                .values()
                .any(|entry| entry.active != 0 || entry.phase != RoutePhase::Active)
        {
            return Err(LlmError::new(
                "LLM_BUSY",
                "service cannot change ownership while active or draining",
            ));
        }
        if identities
            .iter()
            .map(|(id, _)| *id)
            .collect::<BTreeSet<_>>()
            != state.entries.keys().copied().collect()
            || identities.len() != state.entries.len()
        {
            return Err(LlmError::new(
                "LLM_BINDING_CHANGED",
                "adoption snapshot changed",
            ));
        }
        let mut keys = BTreeSet::new();
        for (_, identity) in &identities {
            let key = identity.validate()?;
            if state.issued.contains(&key) || !keys.insert(key) {
                return Err(LlmError::new(
                    "LLM_REGISTRATION_ID_REUSED",
                    "adopted identities must be fresh",
                ));
            }
        }
        let end = state
            .next
            .checked_add(identities.len() as u64)
            .ok_or_else(|| LlmError::new("LLM_GENERATION_EXHAUSTED", "generation exhausted"))?;
        for (offset, (id, identity)) in identities.into_iter().enumerate() {
            let generation = state.next + offset as u64 + 1;
            let entry = state
                .entries
                .get_mut(&id)
                .expect("validated adoption entry");
            entry.generation = generation;
            entry.identity = identity;
        }
        state.next = end;
        state.issued.extend(keys);
        Ok(state
            .entries
            .iter()
            .map(|(id, entry)| self.inner.snapshot(*id, entry))
            .collect())
    }
}

pub struct Prepared<T: ?Sized> {
    inner: Arc<Inner<T>>,
    snapshot: RouteSnapshot<T>,
}

impl<T: ?Sized> Prepared<T> {
    pub fn snapshot(&self) -> &RouteSnapshot<T> {
        &self.snapshot
    }

    pub fn ensure_current(&self) -> Result<(), LlmError> {
        let state = self.inner.lock()?;
        self.inner
            .current(&state, self.snapshot.binding)
            .map(|_| ())
    }

    pub fn admit(self) -> Result<Admission<T>, LlmError> {
        {
            let mut state = self.inner.lock()?;
            self.inner.current(&state, self.snapshot.binding)?;
            let entry = state
                .entries
                .get_mut(&self.snapshot.binding.registration)
                .expect("admitted route exists");
            entry.active = entry.active.checked_add(1).ok_or_else(|| {
                LlmError::new("LLM_ADMISSION_EXHAUSTED", "active call counter exhausted")
            })?;
        }
        Ok(Admission {
            inner: self.inner,
            snapshot: self.snapshot,
        })
    }
}

pub struct Admission<T: ?Sized> {
    inner: Arc<Inner<T>>,
    snapshot: RouteSnapshot<T>,
}

impl<T: ?Sized> Admission<T> {
    pub fn snapshot(&self) -> &RouteSnapshot<T> {
        &self.snapshot
    }
}

impl<T: ?Sized> Drop for Admission<T> {
    fn drop(&mut self) {
        let mut retired = None;
        if let Ok(mut state) = self.inner.lock()
            && let Some(entry) = state.entries.get_mut(&self.snapshot.binding.registration)
        {
            entry.active -= 1;
            if entry.detached && entry.active == 0 {
                retired = state.entries.remove(&self.snapshot.binding.registration);
            }
        }
        drop(retired);
    }
}
