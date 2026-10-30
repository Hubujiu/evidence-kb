use crate::{Error, Manifest, manifest};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
    Create {
        plugins: Vec<Manifest>,
        #[serde(default)]
        parent: Option<u64>,
    },
    Activate {
        host: u64,
        plugin: String,
    },
    Ready {
        host: u64,
    },
    Resolve {
        host: u64,
        service: String,
    },
    Acquire {
        host: u64,
    },
    Release {
        host: u64,
        lease: u64,
    },
    Drain {
        host: u64,
    },
    Dispose {
        host: u64,
    },
    Abort {
        host: u64,
    },
    FinishDispose {
        host: u64,
    },
    Inspect {
        host: u64,
    },
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Starting,
    Ready,
    Draining,
    Disposing,
}

impl Phase {
    fn name(self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Ready => "ready",
            Self::Draining => "draining",
            Self::Disposing => "disposing",
        }
    }
}

struct Host {
    parent: Option<u64>,
    plugins: Vec<Manifest>,
    providers: BTreeMap<String, usize>,
    order: Vec<String>,
    activated: Vec<String>,
    active_plugins: BTreeSet<String>,
    phase: Phase,
    leases: BTreeSet<u64>,
    children: BTreeSet<u64>,
}

#[derive(Default)]
pub struct HostRegistry {
    next_id: u64,
    hosts: BTreeMap<u64, Host>,
}

impl HostRegistry {
    fn allocate(&mut self) -> Result<u64, Error> {
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| Error::new("handle_limit", "native handle range exhausted"))?;
        Ok(self.next_id)
    }

    fn host(&self, id: u64) -> Result<&Host, Error> {
        self.hosts
            .get(&id)
            .ok_or_else(|| Error::new("unknown_host", "unknown host handle"))
    }

    fn host_mut(&mut self, id: u64) -> Result<&mut Host, Error> {
        self.hosts
            .get_mut(&id)
            .ok_or_else(|| Error::new("unknown_host", "unknown host handle"))
    }

    fn ancestors_ready(&self, mut parent: Option<u64>) -> Result<(), Error> {
        while let Some(id) = parent {
            let host = self.host(id)?;
            if host.phase != Phase::Ready {
                return Err(Error::new(
                    "scope_unavailable",
                    "scope or ancestor is not ready",
                ));
            }
            parent = host.parent;
        }
        Ok(())
    }

    fn inherited(&self, mut parent: Option<u64>) -> Result<BTreeSet<String>, Error> {
        let mut services = BTreeSet::new();
        while let Some(id) = parent {
            let host = self.host(id)?;
            services.extend(host.providers.keys().cloned());
            parent = host.parent;
        }
        Ok(services)
    }

    fn create(&mut self, plugins: Vec<Manifest>, parent: Option<u64>) -> Result<Value, Error> {
        if self.hosts.len() >= 4096 {
            return Err(Error::new("host_limit", "too many live scopes"));
        }
        self.ancestors_ready(parent)?;
        let order = manifest::order(&plugins, &self.inherited(parent)?)?;
        let providers = plugins
            .iter()
            .enumerate()
            .flat_map(|(index, plugin)| {
                plugin
                    .provides
                    .iter()
                    .cloned()
                    .map(move |name| (name, index))
            })
            .collect();
        let id = self.allocate()?;
        self.hosts.insert(
            id,
            Host {
                parent,
                plugins,
                providers,
                order: order.clone(),
                activated: Vec::new(),
                active_plugins: BTreeSet::new(),
                phase: Phase::Starting,
                leases: BTreeSet::new(),
                children: BTreeSet::new(),
            },
        );
        if let Some(parent) = parent {
            self.host_mut(parent)?.children.insert(id);
        }
        Ok(json!({"host": id, "order": order}))
    }

    fn resolve(&self, mut id: u64, service: &str) -> Result<Value, Error> {
        if !manifest::valid_name(service) {
            return Err(Error::new("invalid_service", "invalid service name"));
        }
        loop {
            let host = self.host(id)?;
            if let Some(index) = host.providers.get(service) {
                let plugin = &host.plugins[*index];
                if !host.active_plugins.contains(&plugin.id) {
                    return Err(Error::new(
                        "service_inactive",
                        "service provider has not activated",
                    ));
                }
                return Ok(json!({"host": id, "plugin": plugin.id}));
            }
            match host.parent {
                Some(parent) => id = parent,
                None => {
                    return Err(Error::new(
                        "missing_service",
                        format!("unknown service {}", service),
                    ));
                }
            }
        }
    }

    fn prepare_dispose(&mut self, id: u64, abort: bool) -> Result<Value, Error> {
        let host = self.host_mut(id)?;
        if !host.children.is_empty() || !host.leases.is_empty() {
            return Err(Error::new(
                "scope_busy",
                "scope has active leases or child scopes",
            ));
        }
        let allowed = if abort {
            host.phase == Phase::Starting
        } else {
            host.phase == Phase::Draining
        };
        if !allowed && host.phase != Phase::Disposing {
            return Err(Error::new(
                "invalid_phase",
                "drain or abort scope before disposal",
            ));
        }
        host.phase = Phase::Disposing;
        Ok(json!({"order": host.activated.iter().rev().collect::<Vec<_>>()}))
    }

    pub fn request(&mut self, request: Value) -> Result<Value, Error> {
        let command: Command = serde_json::from_value(request)
            .map_err(|error| Error::new("invalid_request", error.to_string()))?;
        match command {
            Command::Create { plugins, parent } => self.create(plugins, parent),
            Command::Activate { host: id, plugin } => {
                let parent = self.host(id)?.parent;
                self.ancestors_ready(parent)?;
                let host = self.host_mut(id)?;
                if host.phase != Phase::Starting
                    || host.order.get(host.activated.len()) != Some(&plugin)
                {
                    return Err(Error::new(
                        "activation_order",
                        "plugin activation must follow planned order",
                    ));
                }
                host.activated.push(plugin.clone());
                host.active_plugins.insert(plugin.clone());
                Ok(json!({"plugin": plugin}))
            }
            Command::Ready { host: id } => {
                self.ancestors_ready(self.host(id)?.parent)?;
                let host = self.host_mut(id)?;
                if host.phase != Phase::Starting || host.activated.len() != host.plugins.len() {
                    return Err(Error::new(
                        "invalid_phase",
                        "all plugins must activate before ready",
                    ));
                }
                host.phase = Phase::Ready;
                Ok(json!({"state": "ready"}))
            }
            Command::Resolve { host, service } => self.resolve(host, &service),
            Command::Acquire { host } => {
                self.ancestors_ready(Some(host))?;
                let lease = self.allocate()?;
                self.host_mut(host)?.leases.insert(lease);
                Ok(json!({"lease": lease}))
            }
            Command::Release { host, lease } => {
                if !self.host_mut(host)?.leases.remove(&lease) {
                    return Err(Error::new(
                        "unknown_lease",
                        "lease does not belong to this scope",
                    ));
                }
                Ok(json!({}))
            }
            Command::Drain { host } => {
                let host = self.host_mut(host)?;
                if !matches!(host.phase, Phase::Ready | Phase::Draining) {
                    return Err(Error::new("invalid_phase", "only ready scope can drain"));
                }
                host.phase = Phase::Draining;
                Ok(json!({"leases": host.leases.len(), "children": host.children.len()}))
            }
            Command::Dispose { host } => self.prepare_dispose(host, false),
            Command::Abort { host } => self.prepare_dispose(host, true),
            Command::FinishDispose { host: id } => {
                let host = self.host(id)?;
                if host.phase != Phase::Disposing {
                    return Err(Error::new(
                        "invalid_phase",
                        "scope must finish resource disposal first",
                    ));
                }
                let parent = host.parent;
                self.hosts.remove(&id);
                if let Some(parent) = parent {
                    self.host_mut(parent)?.children.remove(&id);
                }
                Ok(json!({"state": "disposed"}))
            }
            Command::Inspect { host: id } => {
                let host = self.host(id)?;
                Ok(
                    json!({"host": id, "parent": host.parent, "state": host.phase.name(),
                    "order": host.order, "activated": host.activated, "plugins": host.plugins,
                    "leases": host.leases.len(), "children": host.children}),
                )
            }
        }
    }
}
