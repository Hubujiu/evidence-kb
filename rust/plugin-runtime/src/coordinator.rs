use crate::{
    Action, Binding, Command, Completion, Desired, Error, Manifest, PROTOCOL, Phase, manifest,
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

struct Scope {
    runtime: u64,
    parent: Option<u64>,
    children: BTreeSet<u64>,
    order: Vec<String>,
    instances: BTreeMap<String, u64>,
    closing: Option<Desired>,
    desired_revision: u64,
    observed_revision: u64,
    affected: Vec<u64>,
    isolated: BTreeSet<String>,
    intercept: Value,
}

struct Instance {
    id: u64,
    host: u64,
    manifest: Manifest,
    next_manifest: Option<Manifest>,
    generation: u64,
    desired: Desired,
    phase: Phase,
    bindings: BTreeMap<String, Binding>,
    registrations: BTreeMap<String, u64>,
    resources: Vec<Resource>,
    init: Option<u64>,
    cleanup: Option<u64>,
    failed_init: bool,
    failure: Option<String>,
}

struct Resource {
    id: u64,
    kind: String,
    done: bool,
    event: Option<(String, i32)>,
}

struct Operation {
    action: Action,
    completion: Option<Completion>,
    claimed: bool,
}

struct Lease {
    host: u64,
    owners: BTreeSet<u64>,
    registrations: BTreeSet<u64>,
    execution_owner: Option<u64>,
    ancestors: BTreeSet<u64>,
    deadline: Option<u64>,
    cancelled: bool,
}

struct Update {
    enabled: Option<bool>,
    remove: bool,
    config: Option<Value>,
    manifest: Option<Manifest>,
    configuration_inputs: Option<BTreeMap<String, String>>,
}

#[derive(Default)]
pub struct Coordinator {
    next_id: u64,
    scopes: BTreeMap<u64, Scope>,
    instances: BTreeMap<u64, Instance>,
    operations: BTreeMap<u64, Operation>,
    actions: VecDeque<Action>,
    leases: BTreeMap<u64, Lease>,
    diagnostics: Vec<Value>,
    retired: BTreeSet<u64>,
    now: u64,
}

impl Coordinator {
    fn allocate(&mut self) -> Result<u64, Error> {
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| Error::new("handle_limit", "native handle range exhausted"))?;
        Ok(self.next_id)
    }

    fn scope(&self, host: u64) -> Result<&Scope, Error> {
        self.scopes
            .get(&host)
            .ok_or_else(|| Error::new("unknown_host", "unknown host handle"))
    }

    fn is_ancestor(&self, ancestor: u64, mut child: u64) -> bool {
        loop {
            if ancestor == child {
                return true;
            }
            let Some(parent) = self.scopes[&child].parent else {
                return false;
            };
            child = parent;
        }
    }

    fn open_ancestors(&self, mut host: Option<u64>) -> Result<(), Error> {
        while let Some(id) = host {
            let scope = self.scope(id)?;
            if scope.closing.is_some() {
                return Err(Error::new(
                    "scope_unavailable",
                    "scope or ancestor is draining",
                ));
            }
            host = scope.parent;
        }
        Ok(())
    }

    fn inherited(&self, mut parent: Option<u64>) -> Result<BTreeSet<String>, Error> {
        let mut result = BTreeSet::new();
        let mut blocked = BTreeSet::new();
        while let Some(host) = parent {
            let scope = self.scope(host)?;
            for id in scope.instances.values() {
                let instance = &self.instances[id];
                if instance.desired != Desired::Removed {
                    result.extend(
                        instance
                            .manifest
                            .provides
                            .iter()
                            .filter(|service| !blocked.contains(*service))
                            .cloned(),
                    );
                }
            }
            blocked.extend(scope.isolated.iter().cloned());
            parent = scope.parent;
        }
        Ok(result)
    }

    fn binding(&self, call_scope: u64, service: &str) -> Result<Binding, Error> {
        self.lookup_binding(call_scope, service, false)
    }

    fn intercept(&self, call: u64, definition: u64, service: &str) -> Value {
        let mut chain = Vec::new();
        let mut current = Some(call);
        while let Some(host) = current {
            chain.push(host);
            if host == definition {
                break;
            }
            current = self.scopes[&host].parent;
        }
        let mut result = json!({});
        for host in chain.into_iter().rev() {
            if let Some(overlay) = self.scopes[&host].intercept.get(service) {
                merge(&mut result, overlay);
            }
        }
        result
    }

    fn scope_options(
        &self,
        isolated: Vec<String>,
        intercept: &Value,
    ) -> Result<BTreeSet<String>, Error> {
        let count = isolated.len();
        let isolated: BTreeSet<_> = isolated.into_iter().collect();
        if count > 256
            || isolated.len() != count
            || !isolated.iter().all(|name| manifest::valid_name(name))
            || !intercept.as_object().is_some_and(|object| {
                object
                    .iter()
                    .all(|(name, value)| manifest::valid_name(name) && value.is_object())
            })
        {
            return Err(Error::new(
                "invalid_scope",
                "isolation needs distinct service names and intercept needs service object overlays",
            ));
        }
        Ok(isolated)
    }

    fn lookup_binding(
        &self,
        call_scope: u64,
        service: &str,
        retained: bool,
    ) -> Result<Binding, Error> {
        if !manifest::valid_name(service) {
            return Err(Error::new("invalid_service", "invalid service name"));
        }
        let mut current = Some(call_scope);
        while let Some(host) = current {
            let scope = self.scope(host)?;
            for id in scope.instances.values() {
                let instance = &self.instances[id];
                if instance.phase == Phase::Disposed
                    || !instance
                        .manifest
                        .provides
                        .iter()
                        .any(|name| name == service)
                {
                    continue;
                }
                if instance.phase != Phase::Active
                    && !(retained && instance.registrations.contains_key(service))
                {
                    return Err(Error::new(
                        "service_inactive",
                        format!("{} is not active", instance.manifest.id),
                    ));
                }
                return Ok(Binding {
                    runtime: scope.runtime,
                    host,
                    plugin: instance.manifest.id.clone(),
                    instance: *id,
                    generation: instance.generation,
                    registration: instance.registrations[service],
                    service: service.into(),
                    definition_scope: host,
                    call_scope,
                    intercept: self.intercept(call_scope, host, service),
                });
            }
            if scope.isolated.contains(service) {
                break;
            }
            current = scope.parent;
        }
        Err(Error::new(
            "missing_service",
            format!("unknown service {service}"),
        ))
    }

    fn action(
        &mut self,
        id: u64,
        kind: &str,
        resource: Option<u64>,
        lease: Option<u64>,
        method: Option<String>,
    ) -> Result<Action, Error> {
        let operation = self.allocate()?;
        let instance = &self.instances[&id];
        let action = Action {
            kind: kind.into(),
            runtime: self.scopes[&instance.host].runtime,
            host: instance.host,
            instance: id,
            plugin: instance.manifest.id.clone(),
            generation: instance.generation,
            operation,
            config: instance.manifest.config.clone(),
            bindings: instance.bindings.clone(),
            resource,
            deadline: lease.and_then(|lease| self.leases[&lease].deadline),
            cancelled: false,
            lease,
            method,
            implementation: instance.manifest.implementation.clone(),
            binding: None,
            configuration_inputs: instance.manifest.configuration_inputs.clone(),
        };
        self.operations.insert(
            operation,
            Operation {
                action: action.clone(),
                completion: None,
                claimed: false,
            },
        );
        Ok(action)
    }

    fn drain_instance(&mut self, id: u64) -> bool {
        let instance = self.instances.get_mut(&id).unwrap();
        if matches!(instance.phase, Phase::Starting | Phase::Active) {
            instance.phase = Phase::Draining;
            self.cancel_unclaimed_init(id);
            let pending: Vec<_> = self
                .operations
                .values_mut()
                .filter(|operation| {
                    operation.action.instance == id && operation.completion.is_none()
                })
                .map(|operation| {
                    operation.action.cancelled = true;
                    let mut action = operation.action.clone();
                    action.kind = "cancel".into();
                    action
                })
                .collect();
            self.actions.extend(pending);
            for lease in self
                .leases
                .values_mut()
                .filter(|lease| lease.owners.contains(&id))
            {
                lease.cancelled = true;
            }
            true
        } else {
            false
        }
    }

    fn cancel_unclaimed_init(&mut self, id: u64) {
        if let Some(operation) = self.instances[&id].init
            && !self.operations[&operation].claimed
        {
            self.operations.remove(&operation);
            self.instances.get_mut(&id).unwrap().init = None;
            self.actions.retain(|action| action.operation != operation);
        }
    }

    fn retained_by_consumer(&self, id: u64) -> bool {
        self.instances.values().any(|consumer| {
            consumer.id != id
                && consumer
                    .bindings
                    .values()
                    .any(|binding| binding.instance == id)
        })
    }

    fn closing_scope_busy(&self, host: u64) -> bool {
        let mut current = Some(host);
        while let Some(id) = current {
            if self.scopes[&id].closing.is_some() {
                let hosts: BTreeSet<_> = self.subtree(id).unwrap().into_iter().collect();
                if self
                    .leases
                    .values()
                    .any(|lease| hosts.contains(&lease.host))
                {
                    return true;
                }
            }
            current = self.scopes[&id].parent;
        }
        false
    }

    fn reconcile(&mut self) -> Result<(), Error> {
        loop {
            let mut changed = false;
            let invalid: Vec<_> = self
                .instances
                .values()
                .filter(|instance| {
                    matches!(instance.phase, Phase::Starting | Phase::Active)
                        && instance.bindings.values().any(|binding| {
                            self.instances
                                .get(&binding.instance)
                                .is_none_or(|provider| {
                                    provider.phase != Phase::Active
                                        || provider.generation != binding.generation
                                        || provider.desired != Desired::Enabled
                                })
                        })
                })
                .map(|instance| instance.id)
                .collect();
            for id in invalid {
                changed |= self.drain_instance(id);
            }

            let cleaning: Vec<_> = self
                .instances
                .values()
                .filter(|instance| {
                    instance.phase == Phase::Draining
                        && instance.init.is_none()
                        && !self
                            .leases
                            .values()
                            .any(|lease| lease.owners.contains(&instance.id))
                        && !self.retained_by_consumer(instance.id)
                        && !self.closing_scope_busy(instance.host)
                })
                .map(|instance| instance.id)
                .collect();
            for id in cleaning {
                self.instances.get_mut(&id).unwrap().phase = Phase::Cleaning;
                changed = true;
            }

            let scheduling: Vec<_> = self
                .instances
                .values()
                .filter(|instance| instance.phase == Phase::Cleaning && instance.cleanup.is_none())
                .map(|instance| instance.id)
                .collect();
            for id in scheduling {
                let resource = self.instances[&id]
                    .resources
                    .iter()
                    .rev()
                    .find(|resource| !resource.done)
                    .map_or(0, |resource| resource.id);
                let action = self.action(id, "cleanup", Some(resource), None, None)?;
                self.instances.get_mut(&id).unwrap().cleanup = Some(action.operation);
                self.actions.push_back(action);
            }

            let pending: Vec<_> = self
                .instances
                .values()
                .filter(|instance| {
                    instance.phase == Phase::Pending
                        && instance.desired == Desired::Enabled
                        && !instance.failed_init
                        && self.open_ancestors(Some(instance.host)).is_ok()
                })
                .map(|instance| instance.id)
                .collect();
            for id in pending {
                let instance = &self.instances[&id];
                let mut bindings = BTreeMap::new();
                for service in &instance.manifest.requires {
                    if let Ok(binding) = self.binding(instance.host, service) {
                        bindings.insert(service.clone(), binding);
                    }
                }
                if bindings.len() != instance.manifest.requires.len() {
                    continue;
                }
                let instance = self.instances.get_mut(&id).unwrap();
                instance.bindings = bindings;
                instance.phase = Phase::Starting;
                let action = self.action(id, "init", None, None, None)?;
                self.instances.get_mut(&id).unwrap().init = Some(action.operation);
                self.actions.push_back(action);
                changed = true;
            }

            let removed: Vec<_> = self
                .instances
                .values()
                .filter(|instance| {
                    instance.desired == Desired::Removed
                        && matches!(instance.phase, Phase::Pending | Phase::Failed)
                })
                .map(|instance| instance.id)
                .collect();
            for id in removed {
                self.instances.get_mut(&id).unwrap().phase = Phase::Disposed;
                changed = true;
            }
            let finished_children: Vec<_> = self
                .scopes
                .iter()
                .filter(|(host, scope)| {
                    scope.closing == Some(Desired::Removed)
                        && scope.children.is_empty()
                        && scope
                            .instances
                            .values()
                            .all(|id| self.instances[id].phase == Phase::Disposed)
                        && !self.leases.values().any(|lease| lease.host == **host)
                })
                .filter_map(|(host, scope)| scope.parent.map(|parent| (*host, parent)))
                .collect();
            for (host, parent) in finished_children {
                if self.scopes.get_mut(&parent).unwrap().children.remove(&host) {
                    changed = true;
                }
            }
            for scope in self.scopes.values_mut() {
                if scope
                    .instances
                    .values()
                    .chain(scope.affected.iter())
                    .all(|id| {
                        let Some(instance) = self.instances.get(id) else {
                            return true;
                        };
                        match instance.desired {
                            Desired::Enabled => instance.phase == Phase::Active,
                            Desired::Disabled => instance.phase == Phase::Pending,
                            Desired::Removed => instance.phase == Phase::Disposed,
                        }
                    })
                {
                    scope.observed_revision = scope.desired_revision;
                }
            }
            if !changed {
                break;
            }
        }
        Ok(())
    }

    fn create(
        &mut self,
        plugins: Vec<Manifest>,
        parent: Option<u64>,
        isolated: BTreeSet<String>,
        intercept: Value,
    ) -> Result<Value, Error> {
        self.open_ancestors(parent)?;
        let inherited = self
            .inherited(parent)?
            .difference(&isolated)
            .cloned()
            .collect();
        let order = manifest::order(&plugins, &inherited)?;
        if self
            .scopes
            .values()
            .filter(|scope| scope.closing != Some(Desired::Removed))
            .count()
            >= 4096
        {
            return Err(Error::new("host_limit", "too many live scopes"));
        }
        let host = self.allocate()?;
        let runtime = parent.map_or(host, |parent| self.scopes[&parent].runtime);
        let mut instances = BTreeMap::new();
        for manifest in plugins {
            let id = self.allocate()?;
            instances.insert(manifest.id.clone(), id);
            self.instances.insert(
                id,
                Instance {
                    id,
                    host,
                    manifest,
                    next_manifest: None,
                    generation: 1,
                    desired: Desired::Enabled,
                    phase: Phase::Pending,
                    bindings: BTreeMap::new(),
                    registrations: BTreeMap::new(),
                    resources: Vec::new(),
                    init: None,
                    cleanup: None,
                    failed_init: false,
                    failure: None,
                },
            );
        }
        self.scopes.insert(
            host,
            Scope {
                runtime,
                parent,
                children: BTreeSet::new(),
                order: order.clone(),
                instances,
                closing: None,
                desired_revision: 1,
                observed_revision: 0,
                affected: Vec::new(),
                isolated,
                intercept,
            },
        );
        if let Some(parent) = parent {
            self.scopes.get_mut(&parent).unwrap().children.insert(host);
        }
        self.reconcile()?;
        let instances: Vec<_> = self.scopes[&host]
            .instances
            .iter()
            .map(|(plugin, id)| json!({"plugin":plugin,"instance":id,"generation":1}))
            .collect();
        Ok(json!({"host":host,"runtime":runtime,"order":order,"instances":instances}))
    }

    fn complete(
        &mut self,
        host: u64,
        operation: u64,
        completion: Completion,
    ) -> Result<Value, Error> {
        self.scope(host)?;
        if !matches!(completion.status.as_str(), "ok" | "error") {
            return Err(Error::new(
                "invalid_completion",
                "status must be ok or error",
            ));
        }
        let record = self
            .operations
            .get(&operation)
            .ok_or_else(|| Error::new("unknown_operation", "unknown operation"))?;
        if record.action.host != host {
            return Err(Error::new(
                "operation_scope",
                "operation belongs to another scope",
            ));
        }
        if let Some(existing) = &record.completion {
            if existing == &completion {
                return Ok(json!({"duplicate":true,"state":self.state(host)?}));
            }
            self.diagnostic(
                host,
                "conflicting_completion".into(),
                "accepted completion cannot be overwritten".into(),
                None,
                Some(operation),
            )?;
            return Err(Error::new(
                "conflicting_completion",
                "accepted completion cannot be overwritten",
            ));
        }
        if !record.claimed {
            return Err(Error::new(
                "operation_unclaimed",
                "poll operation before completing it",
            ));
        }
        let action = record.action.clone();
        let id = action.instance;
        let success = completion.status == "ok";
        if action.kind == "init" && success {
            let expected: BTreeSet<_> = self.instances[&id].manifest.provides.iter().collect();
            if expected != completion.services.iter().collect()
                || completion.services.len() != expected.len()
            {
                return Err(Error::new(
                    "incomplete_services",
                    "init completion must contain exactly the declared services",
                ));
            }
        }
        self.operations.get_mut(&operation).unwrap().completion = Some(completion.clone());
        match action.kind.as_str() {
            "init" => {
                let publish = success
                    && self.instances[&id].phase == Phase::Starting
                    && self.instances[&id].desired == Desired::Enabled
                    && self.open_ancestors(Some(host)).is_ok()
                    && self.instances[&id].bindings.values().all(|binding| {
                        self.instances[&binding.instance].phase == Phase::Active
                            && self.instances[&binding.instance].generation == binding.generation
                    });
                let registrations = if publish {
                    let services = self.instances[&id].manifest.provides.clone();
                    let mut registrations = BTreeMap::new();
                    for service in services {
                        registrations.insert(service, self.allocate()?);
                    }
                    registrations
                } else {
                    BTreeMap::new()
                };
                let instance = self.instances.get_mut(&id).unwrap();
                instance.init = None;
                if publish {
                    instance.registrations = registrations;
                    instance.phase = Phase::Active;
                } else {
                    instance.phase = Phase::Draining;
                    if !success {
                        instance.failed_init = true;
                        instance.failure = completion
                            .error
                            .clone()
                            .or_else(|| Some("initialization failed".into()));
                    }
                }
            }
            "cleanup" => {
                let instance = self.instances.get_mut(&id).unwrap();
                instance.cleanup = None;
                if !success {
                    instance.phase = Phase::CleanupFailed;
                    instance.failure = completion
                        .error
                        .clone()
                        .or_else(|| Some("cleanup failed".into()));
                } else if action.resource == Some(0) {
                    instance.resources.clear();
                    instance.bindings.clear();
                    instance.registrations.clear();
                    if let Some(manifest) = instance.next_manifest.take() {
                        instance.manifest = manifest;
                        instance.failed_init = false;
                    }
                    instance.phase = match instance.desired {
                        Desired::Removed => Phase::Disposed,
                        _ if instance.failed_init => Phase::Failed,
                        _ => Phase::Pending,
                    };
                    if instance.phase == Phase::Pending {
                        instance.generation += 1;
                        instance.failure = None;
                    }
                } else if let Some(resource) = instance
                    .resources
                    .iter_mut()
                    .find(|resource| Some(resource.id) == action.resource)
                {
                    resource.done = true;
                }
            }
            "invoke" => {
                if let Some(lease) = action.lease {
                    self.leases.remove(&lease);
                }
            }
            _ => {
                return Err(Error::new(
                    "invalid_operation",
                    "operation kind cannot complete",
                ));
            }
        }
        self.reconcile()?;
        Ok(json!({"duplicate":false,"state":self.state(host)?}))
    }

    fn resource(
        &mut self,
        host: u64,
        operation: u64,
        kind: String,
        event: Option<(String, i32)>,
    ) -> Result<Value, Error> {
        if !manifest::valid_name(&kind) {
            return Err(Error::new("invalid_resource", "invalid resource kind"));
        }
        let record = self
            .operations
            .get(&operation)
            .ok_or_else(|| Error::new("unknown_operation", "unknown operation"))?;
        if record.action.host != host
            || !matches!(record.action.kind.as_str(), "init" | "invoke")
            || record.completion.is_some()
            || !record.claimed
            || self.instances[&record.action.instance].generation != record.action.generation
        {
            return Err(Error::new(
                "registration_closed",
                "resources require a claimed live initialization or invocation action at its owning generation",
            ));
        }
        let instance = record.action.instance;
        let resource = self.allocate()?;
        self.instances
            .get_mut(&instance)
            .unwrap()
            .resources
            .push(Resource {
                id: resource,
                kind,
                done: false,
                event,
            });
        Ok(
            json!({"resource":resource,"instance":instance,"generation":self.instances[&instance].generation}),
        )
    }

    fn resolve(
        &self,
        host: u64,
        service: &str,
        consumer: Option<u64>,
        cleanup: Option<u64>,
        parent_lease: Option<u64>,
        consumer_generation: Option<u64>,
    ) -> Result<Value, Error> {
        self.scope(host)?;
        if let Some(lease) = parent_lease
            && self
                .leases
                .get(&lease)
                .is_none_or(|lease| lease.host != host)
        {
            return Err(Error::new(
                "unknown_lease",
                "parent lease does not belong to scope",
            ));
        }
        if let Some(consumer) = consumer {
            let instance = self
                .instances
                .get(&consumer)
                .ok_or_else(|| Error::new("unknown_instance", "unknown consumer"))?;
            if consumer_generation != Some(instance.generation) {
                return Err(Error::new(
                    "stale_generation",
                    "consumer generation is not current",
                ));
            }
            if instance.host != host
                || !instance
                    .manifest
                    .requires
                    .iter()
                    .any(|name| name == service)
            {
                return Err(Error::new(
                    "undeclared_dependency",
                    "consumer did not declare service dependency",
                ));
            }
            if let Some(operation) = cleanup {
                let action = &self
                    .operations
                    .get(&operation)
                    .ok_or_else(|| Error::new("unknown_operation", "unknown cleanup action"))?
                    .action;
                if action.instance != consumer
                    || action.kind != "cleanup"
                    || instance.cleanup != Some(operation)
                {
                    return Err(Error::new(
                        "invalid_cleanup_access",
                        "cleanup dependency access belongs to current cleanup action",
                    ));
                }
            } else if !matches!(instance.phase, Phase::Starting | Phase::Active) {
                return Err(Error::new("service_inactive", "consumer is not active"));
            }
            let binding = instance
                .bindings
                .get(service)
                .ok_or_else(|| Error::new("missing_service", "dependency is not bound"))?;
            return Ok(json!(binding));
        }
        if cleanup.is_some() {
            return Err(Error::new(
                "invalid_cleanup_access",
                "cleanup lookup requires consumer identity",
            ));
        }
        Ok(json!(self.lookup_binding(host, service, true)?))
    }

    fn acquire(
        &mut self,
        host: u64,
        registration: Option<u64>,
        owner: Option<u64>,
        generation: Option<u64>,
        parent_lease: Option<u64>,
        deadline: Option<u64>,
    ) -> Result<Value, Error> {
        self.scope(host)?;
        if let Some(owner) = owner {
            let instance = self
                .instances
                .get(&owner)
                .ok_or_else(|| Error::new("unknown_instance", "unknown lease owner"))?;
            if generation != Some(instance.generation) {
                return Err(Error::new(
                    "stale_generation",
                    "owner generation is not current",
                ));
            }
        }
        let parent = if let Some(parent) = parent_lease {
            let lease = self
                .leases
                .get(&parent)
                .ok_or_else(|| Error::new("unknown_lease", "parent lease is no longer live"))?;
            if lease.host != host
                && !(registration.is_none()
                    && owner.is_some_and(|owner| lease.owners.contains(&owner))
                    && self.scopes[&lease.host].runtime == self.scopes[&host].runtime
                    && self.is_ancestor(host, lease.host))
            {
                return Err(Error::new(
                    "lease_scope",
                    "parent lease belongs to another scope",
                ));
            }
            Some(lease)
        } else {
            self.open_ancestors(Some(host))?;
            if owner.is_none()
                && self.scopes[&host].observed_revision == 0
                && self.scopes[&host]
                    .instances
                    .values()
                    .any(|id| self.instances[id].phase == Phase::Starting)
            {
                return Err(Error::new(
                    "scope_unavailable",
                    "scope initial activation is incomplete",
                ));
            }
            None
        };
        let mut owners = parent.map_or_else(BTreeSet::new, |lease| lease.owners.clone());
        let mut registrations =
            parent.map_or_else(BTreeSet::new, |lease| lease.registrations.clone());
        let mut ancestors = parent.map_or_else(BTreeSet::new, |lease| lease.ancestors.clone());
        ancestors.extend(parent_lease);
        let owner = if let Some(registration) = registration {
            Some(
                self.instances
                    .values()
                    .find(|instance| {
                        instance
                            .registrations
                            .values()
                            .any(|id| *id == registration)
                    })
                    .ok_or_else(|| {
                        Error::new("stale_binding", "service registration no longer exists")
                    })?
                    .id,
            )
        } else {
            owner
        };
        if let Some(owner) = owner {
            let instance = self
                .instances
                .get(&owner)
                .ok_or_else(|| Error::new("unknown_instance", "unknown lease owner"))?;
            let scope_runtime = self.scopes[&host].runtime;
            if self.scopes[&instance.host].runtime != scope_runtime {
                return Err(Error::new(
                    "lease_scope",
                    "owner belongs to another runtime",
                ));
            }
            let mut current = Some(host);
            while current.is_some_and(|scope| scope != instance.host) {
                current = self.scopes[&current.unwrap()].parent;
            }
            if current.is_none() {
                return Err(Error::new(
                    "lease_scope",
                    "owner is outside the calling scope ancestry",
                ));
            }
            if let Some(registration) = registration {
                let service = instance
                    .registrations
                    .iter()
                    .find(|(_, id)| **id == registration)
                    .unwrap()
                    .0;
                if !registrations.contains(&registration)
                    && !self
                        .binding(host, service)
                        .is_ok_and(|binding| binding.registration == registration)
                {
                    return Err(Error::new(
                        "stale_binding",
                        "registration is not visible in calling scope",
                    ));
                }
                registrations.insert(registration);
            }
            if instance.phase != Phase::Active && !owners.contains(&owner) {
                return Err(Error::new(
                    "service_inactive",
                    "service no longer accepts calls",
                ));
            }
            if instance.manifest.concurrency == "serial"
                && self.leases.iter().any(|(id, lease)| {
                    lease.execution_owner == Some(owner) && !ancestors.contains(id)
                })
            {
                return Err(Error::new("plugin_busy", "plugin declares serial calls"));
            }
            owners.insert(owner);
        } else {
            let hosts: BTreeSet<_> = self.subtree(host)?.into_iter().collect();
            owners.extend(
                self.instances
                    .values()
                    .filter(|instance| {
                        hosts.contains(&instance.host)
                            && matches!(instance.phase, Phase::Active | Phase::Starting)
                    })
                    .map(|instance| instance.id),
            );
            let mut current = self.scopes[&host].parent;
            while let Some(host) = current {
                owners.extend(
                    self.scopes[&host]
                        .instances
                        .values()
                        .filter(|id| self.instances[id].phase == Phase::Active)
                        .copied(),
                );
                current = self.scopes[&host].parent;
            }
            for service in self.inherited(Some(host))? {
                if let Ok(binding) = self.binding(host, &service) {
                    owners.insert(binding.instance);
                    registrations.insert(binding.registration);
                }
            }
        }
        let deadline = match (deadline, parent.and_then(|lease| lease.deadline)) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        };
        if deadline.is_some_and(|deadline| deadline <= self.now) {
            return Err(Error::new("deadline", "deadline already elapsed"));
        }
        let lease = self.allocate()?;
        self.leases.insert(
            lease,
            Lease {
                host,
                owners,
                registrations,
                execution_owner: owner,
                ancestors,
                deadline,
                cancelled: false,
            },
        );
        Ok(json!({"lease":lease,"deadline":deadline}))
    }

    fn invoke(
        &mut self,
        host: u64,
        registration: u64,
        method: String,
        parent_lease: Option<u64>,
        deadline: Option<u64>,
    ) -> Result<Value, Error> {
        if !manifest::valid_name(&method) {
            return Err(Error::new("invalid_method", "invalid invocation method"));
        }
        let id = self
            .instances
            .values()
            .find(|instance| {
                instance
                    .registrations
                    .values()
                    .any(|value| *value == registration)
            })
            .ok_or_else(|| Error::new("stale_binding", "service registration no longer exists"))?
            .id;
        let admitted =
            self.acquire(host, Some(registration), None, None, parent_lease, deadline)?;
        let lease = admitted["lease"].as_u64().unwrap();
        let mut action = self.action(id, "invoke", None, Some(lease), Some(method))?;
        action.host = host;
        let service = self.instances[&id]
            .registrations
            .iter()
            .find(|(_, id)| **id == registration)
            .unwrap()
            .0;
        action.binding = Some(self.lookup_binding(host, service, true)?);
        self.operations.get_mut(&action.operation).unwrap().action = action.clone();
        self.operations.get_mut(&action.operation).unwrap().claimed = true;
        Ok(json!(action))
    }

    fn subtree(&self, host: u64) -> Result<Vec<u64>, Error> {
        self.scope(host)?;
        let mut result = vec![host];
        let mut offset = 0;
        while offset < result.len() {
            result.extend(self.scopes[&result[offset]].children.iter().copied());
            offset += 1;
        }
        Ok(result)
    }

    fn drain(&mut self, host: u64, remove: bool) -> Result<Value, Error> {
        let desired = if remove {
            Desired::Removed
        } else {
            Desired::Disabled
        };
        for host in self.subtree(host)? {
            self.scopes.get_mut(&host).unwrap().closing = Some(desired);
            let ids: Vec<_> = self.scopes[&host].instances.values().copied().collect();
            for id in ids {
                self.instances.get_mut(&id).unwrap().desired = desired;
                self.drain_instance(id);
            }
        }
        self.reconcile()?;
        self.inspect(host)
    }

    fn update(&mut self, host: u64, plugin: String, change: Update) -> Result<Value, Error> {
        let Update {
            enabled,
            remove,
            config,
            manifest,
            configuration_inputs,
        } = change;
        self.open_ancestors(Some(host))?;
        let id = self
            .scope(host)?
            .instances
            .get(&plugin)
            .copied()
            .ok_or_else(|| Error::new("unknown_plugin", "unknown plugin"))?;
        let current = &self.instances[&id];
        if current.phase == Phase::Disposed {
            return Err(Error::new(
                "instance_disposed",
                "recreate removed plugin with a new instance identity",
            ));
        }
        let mut next = manifest.unwrap_or_else(|| {
            current
                .next_manifest
                .as_ref()
                .unwrap_or(&current.manifest)
                .clone()
        });
        if next.id != plugin {
            return Err(Error::new(
                "invalid_manifest",
                "update cannot change plugin identity",
            ));
        }
        if let Some(config) = config {
            next.config = config;
        }
        if let Some(inputs) = configuration_inputs {
            next.configuration_inputs = inputs;
        }
        let manifests: Vec<_> = self
            .scope(host)?
            .instances
            .values()
            .filter(|other| **other == id || self.instances[other].desired != Desired::Removed)
            .map(|other| {
                if *other == id {
                    next.clone()
                } else {
                    self.instances[other]
                        .next_manifest
                        .as_ref()
                        .unwrap_or(&self.instances[other].manifest)
                        .clone()
                }
            })
            .collect();
        let inherited = self
            .inherited(self.scope(host)?.parent)?
            .difference(&self.scope(host)?.isolated)
            .cloned()
            .collect();
        let order = manifest::order(&manifests, &inherited)?;
        let target = if remove {
            Desired::Removed
        } else if enabled == Some(false) {
            Desired::Disabled
        } else {
            Desired::Enabled
        };
        if target == current.desired
            && next
                == current
                    .next_manifest
                    .as_ref()
                    .unwrap_or(&current.manifest)
                    .clone()
            && current.phase != Phase::Failed
        {
            return self.inspect(host);
        }
        let instance = self.instances.get_mut(&id).unwrap();
        instance.desired = target;
        instance.failed_init = false;
        if matches!(instance.phase, Phase::Pending | Phase::Failed) {
            instance.manifest = next;
            instance.phase = Phase::Pending;
            instance.generation += 1;
        } else {
            instance.next_manifest = Some(next);
            self.drain_instance(id);
        }
        let affected = self.affected_closure(&[id]);
        let scope = self.scopes.get_mut(&host).unwrap();
        scope.order = order;
        scope.desired_revision += 1;
        scope.affected = affected;
        self.reconcile()?;
        self.inspect(host)
    }

    fn retry(&mut self, host: u64, plugin: Option<String>) -> Result<Value, Error> {
        let ids: Vec<_> = self
            .scope(host)?
            .instances
            .iter()
            .filter(|(name, _)| plugin.as_ref().is_none_or(|plugin| *name == plugin))
            .map(|(_, id)| *id)
            .collect();
        if plugin.is_some() && ids.is_empty() {
            return Err(Error::new("unknown_plugin", "unknown plugin"));
        }
        for id in ids {
            let instance = self.instances.get_mut(&id).unwrap();
            match instance.phase {
                Phase::CleanupFailed => instance.phase = Phase::Cleaning,
                Phase::Failed => {
                    instance.phase = Phase::Pending;
                    instance.failed_init = false;
                    instance.failure = None;
                    instance.generation += 1;
                }
                _ => (),
            }
        }
        self.reconcile()?;
        self.inspect(host)
    }

    fn state(&self, host: u64) -> Result<&'static str, Error> {
        let scope = self.scope(host)?;
        let phases: Vec<_> = scope
            .instances
            .values()
            .map(|id| self.instances[id].phase)
            .collect();
        if phases.contains(&Phase::CleanupFailed) {
            return Ok("cleanup_failed");
        }
        if phases.contains(&Phase::Cleaning) {
            return Ok("cleaning");
        }
        if scope.closing == Some(Desired::Removed)
            && phases.iter().all(|phase| *phase == Phase::Disposed)
            && scope.children.is_empty()
            && !self.leases.values().any(|lease| lease.host == host)
        {
            return Ok("disposed");
        }
        if scope.closing.is_some() || phases.contains(&Phase::Draining) {
            return Ok("draining");
        }
        if phases.contains(&Phase::Failed) {
            return Ok("failed");
        }
        if phases.contains(&Phase::Starting) {
            return Ok("starting");
        }
        if scope.instances.values().any(|id| {
            self.instances[id].desired == Desired::Enabled
                && self.instances[id].phase == Phase::Pending
        }) {
            return Ok("pending");
        }
        Ok("ready")
    }

    fn inspect(&self, host: u64) -> Result<Value, Error> {
        let scope = self.scope(host)?;
        let instances: Vec<_> = scope.instances.iter().map(|(plugin, id)| {
            let instance = &self.instances[id];
            json!({"plugin":plugin,"instance":id,"generation":instance.generation,"desired":instance.desired,"state":instance.phase,
                "failure":instance.failure,"bindings":instance.bindings,"registrations":instance.registrations,
                "resources":instance.resources.iter().filter(|resource| !resource.done).map(|resource| json!({"resource":resource.id,"kind":resource.kind})).collect::<Vec<_>>()})
        }).collect();
        let status = if self.state(host)? == "cleanup_failed" || self.state(host)? == "failed" {
            "failed"
        } else if scope.observed_revision == scope.desired_revision {
            "complete"
        } else {
            "pending"
        };
        Ok(
            json!({"host":host,"runtime":scope.runtime,"parent":scope.parent,"state":self.state(host)?,"order":scope.order,
            "instances":instances,"plugins":scope.instances.values().map(|id| self.instances[id].manifest.clone()).collect::<Vec<_>>(),
            "leases":self.leases.values().filter(|lease| lease.host == host).count(),"children":scope.children,
            "desired_revision":scope.desired_revision,"observed_revision":scope.observed_revision,
            "operation":{"status":status,"affected_instances":scope.affected},"diagnostics":self.diagnostics.iter().filter(|diagnostic| diagnostic["runtime"] == scope.runtime).collect::<Vec<_>>(),
            "isolated":scope.isolated,"intercept":scope.intercept,"counts":self.counts(Some(scope.runtime))}),
        )
    }

    fn cancel(&mut self, host: u64, operation: u64) -> Result<Value, Error> {
        let record = self
            .operations
            .get_mut(&operation)
            .ok_or_else(|| Error::new("unknown_operation", "unknown operation"))?;
        if record.action.host != host {
            return Err(Error::new(
                "operation_scope",
                "operation belongs to another scope",
            ));
        }
        if record.completion.is_none() && !record.action.cancelled {
            record.action.cancelled = true;
            let mut cancel = record.action.clone();
            cancel.kind = "cancel".into();
            if let Some(lease) = cancel.lease {
                self.leases.get_mut(&lease).unwrap().cancelled = true;
            }
            self.actions.push_back(cancel);
            if record.action.kind == "init" {
                let instance = self.instances.get_mut(&record.action.instance).unwrap();
                instance.phase = Phase::Draining;
                instance.failed_init = true;
                instance.failure = Some("initialization cancelled".into());
            }
        }
        let id = record.action.instance;
        self.cancel_unclaimed_init(id);
        self.reconcile()?;
        Ok(self.operations.get(&operation).map_or_else(|| json!({"cancelled":true,"complete":true}), |record|
            json!({"cancelled":record.action.cancelled,"complete":record.completion.is_some()})))
    }

    fn listeners(&self, host: u64, event: &str) -> Result<Value, Error> {
        self.open_ancestors(Some(host))?;
        let mut chain = Vec::new();
        let mut current = Some(host);
        while let Some(host) = current {
            chain.push(host);
            current = self.scopes[&host].parent;
        }
        let mut listeners = Vec::new();
        for (scope_order, host) in chain.into_iter().rev().enumerate() {
            for plugin in &self.scopes[&host].order {
                let instance = &self.instances[&self.scopes[&host].instances[plugin]];
                if instance.phase != Phase::Active {
                    continue;
                }
                for resource in &instance.resources {
                    if let Some((name, priority)) = &resource.event
                        && name == event
                        && !resource.done
                    {
                        listeners.push((*priority, scope_order, resource.id, json!({"host":host,"plugin":plugin,"instance":instance.id,"generation":instance.generation,"resource":resource.id})));
                    }
                }
            }
        }
        listeners.sort_by_key(|(priority, scope_order, resource, _)| {
            (*priority, *scope_order, *resource)
        });
        Ok(
            json!({"listeners":listeners.into_iter().map(|(_, _, _, value)| value).collect::<Vec<_>>()}),
        )
    }

    pub fn request(&mut self, mut request: Value) -> Result<Value, Error> {
        let object = request
            .as_object_mut()
            .ok_or_else(|| Error::new("invalid_request", "request must be an object"))?;
        if object
            .remove("protocol")
            .and_then(|value| value.as_str().map(str::to_owned))
            .as_deref()
            != Some(PROTOCOL)
        {
            return Err(Error::new("protocol_version", "expected plugin-runtime/1"));
        }
        let command: Command = serde_json::from_value(request)
            .map_err(|error| Error::new("invalid_request", error.to_string()))?;
        match command {
            Command::RuntimeValidate {
                plugins,
                parent,
                isolated,
                intercept,
            } => {
                self.open_ancestors(parent)?;
                let isolated = self.scope_options(isolated, &intercept)?;
                let inherited = self
                    .inherited(parent)?
                    .difference(&isolated)
                    .cloned()
                    .collect();
                let order = manifest::order(&plugins, &inherited)?;
                let available: BTreeSet<_> = inherited
                    .into_iter()
                    .chain(
                        plugins
                            .iter()
                            .flat_map(|plugin| plugin.provides.iter().cloned()),
                    )
                    .collect();
                for plugin in &plugins {
                    if let Some(service) = plugin
                        .requires
                        .iter()
                        .find(|service| !available.contains(*service))
                    {
                        return Err(Error::new(
                            "missing_service",
                            format!("{} requires {}", plugin.id, service),
                        ));
                    }
                }
                Ok(json!({"order":order}))
            }
            Command::RuntimeCreate {
                plugins,
                parent,
                isolated,
                intercept,
            } => {
                let isolated = self.scope_options(isolated, &intercept)?;
                self.create(plugins, parent, isolated, intercept)
            }
            Command::RuntimePoll { host, limit } => {
                let hosts: BTreeSet<_> = self.subtree(host)?.into_iter().collect();
                if limit == 0 || limit > 1024 {
                    return Err(Error::new(
                        "invalid_limit",
                        "poll limit must be between 1 and 1024",
                    ));
                }
                let mut actions = Vec::new();
                let mut rest = VecDeque::new();
                while let Some(mut action) = self.actions.pop_front() {
                    if hosts.contains(&action.host) && actions.len() < limit {
                        if let Some(record) = self.operations.get_mut(&action.operation)
                            && action.kind != "cancel"
                        {
                            record.claimed = true;
                            action.cancelled = record.action.cancelled;
                        }
                        actions.push(action);
                    } else {
                        rest.push_back(action);
                    }
                }
                self.actions = rest;
                Ok(json!({"actions":actions}))
            }
            Command::RuntimeComplete {
                host,
                operation,
                completion,
            } => self.complete(host, operation, completion),
            Command::RuntimeResource {
                host,
                operation,
                kind,
            } => self.resource(host, operation, kind, None),
            Command::RuntimeResolve {
                host,
                service,
                consumer,
                cleanup,
                parent_lease,
                consumer_generation,
            } => self.resolve(
                host,
                &service,
                consumer,
                cleanup,
                parent_lease,
                consumer_generation,
            ),
            Command::RuntimeAcquire {
                host,
                registration,
                instance,
                generation,
                parent_lease,
                deadline,
            } => self.acquire(
                host,
                registration,
                instance,
                generation,
                parent_lease,
                deadline,
            ),
            Command::RuntimeInvoke {
                host,
                registration,
                method,
                call: _,
                parent_lease,
                deadline,
            } => self.invoke(host, registration, method, parent_lease, deadline),
            Command::RuntimeRelease { host, lease } => {
                if self
                    .leases
                    .get(&lease)
                    .is_none_or(|lease| lease.host != host)
                {
                    return Err(Error::new(
                        "unknown_lease",
                        "lease does not belong to scope",
                    ));
                }
                if self.operations.values().any(|operation| {
                    operation.action.lease == Some(lease) && operation.completion.is_none()
                }) {
                    return Err(Error::new(
                        "operation_pending",
                        "invocation must complete before releasing its lease",
                    ));
                }
                self.leases.remove(&lease);
                self.reconcile()?;
                Ok(json!({}))
            }
            Command::RuntimeDrain { host, remove } => self.drain(host, remove),
            Command::RuntimeUpdate {
                host,
                plugin,
                enabled,
                remove,
                config,
                manifest,
                configuration_inputs,
            } => self.update(
                host,
                plugin,
                Update {
                    enabled,
                    remove,
                    config,
                    manifest: manifest.map(|manifest| *manifest),
                    configuration_inputs,
                },
            ),
            Command::RuntimeRetry { host, plugin } => self.retry(host, plugin),
            Command::RuntimeInspect { host } => self.inspect(host),
            Command::RuntimeCollect { host } => self.collect(host),
            Command::RuntimeAck { host, operations } => self.ack(host, operations),
            Command::RuntimeDiagnostic {
                host,
                code,
                message,
                instance,
                operation,
            } => self.diagnostic(host, code, message, instance, operation),
            Command::RuntimeCancel { host, operation } => self.cancel(host, operation),
            Command::RuntimeTick { host, now } => {
                self.scope(host)?;
                if now < self.now {
                    return Err(Error::new(
                        "clock_regression",
                        "runtime clock cannot go backwards",
                    ));
                }
                self.now = now;
                let expired: Vec<_> = self
                    .operations
                    .values()
                    .filter(|operation| {
                        operation.completion.is_none()
                            && operation
                                .action
                                .deadline
                                .is_some_and(|deadline| deadline <= now)
                    })
                    .map(|operation| (operation.action.host, operation.action.operation))
                    .collect();
                for (host, operation) in expired {
                    self.cancel(host, operation)?;
                }
                Ok(json!({"now":now}))
            }
            Command::RuntimeListener {
                host,
                operation,
                event,
                priority,
            } => {
                if !manifest::valid_name(&event) {
                    return Err(Error::new("invalid_event", "invalid event name"));
                }
                self.resource(host, operation, "listener".into(), Some((event, priority)))
            }
            Command::RuntimeListeners { host, event } => self.listeners(host, &event),
            Command::RuntimeApply { host, plugins } => self.apply(host, plugins),
        }
    }

    fn apply(&mut self, host: u64, plugins: Vec<Manifest>) -> Result<Value, Error> {
        self.open_ancestors(Some(host))?;
        let inherited = self
            .inherited(self.scope(host)?.parent)?
            .difference(&self.scope(host)?.isolated)
            .cloned()
            .collect();
        let order = manifest::order(&plugins, &inherited)?;
        let desired: BTreeMap<_, _> = plugins
            .into_iter()
            .map(|manifest| (manifest.id.clone(), manifest))
            .collect();
        let terminal: BTreeSet<_> = self
            .scope(host)?
            .instances
            .values()
            .filter(|id| self.instances[id].phase == Phase::Disposed)
            .copied()
            .collect();
        self.scopes
            .get_mut(&host)
            .unwrap()
            .instances
            .retain(|_, id| !terminal.contains(id));
        self.retired.extend(terminal);
        let existing = self.scope(host)?.instances.clone();
        let mut affected = Vec::new();
        for (plugin, id) in existing {
            let instance = self.instances.get_mut(&id).unwrap();
            if let Some(manifest) = desired.get(&plugin) {
                if instance.desired == Desired::Enabled
                    && instance
                        .next_manifest
                        .as_ref()
                        .unwrap_or(&instance.manifest)
                        == manifest
                {
                    continue;
                }
                if instance.phase == Phase::Disposed {
                    continue;
                }
                instance.desired = Desired::Enabled;
                instance.failed_init = false;
                if matches!(instance.phase, Phase::Pending | Phase::Failed) {
                    instance.manifest = manifest.clone();
                    instance.phase = Phase::Pending;
                    instance.generation += 1;
                } else {
                    instance.next_manifest = Some(manifest.clone());
                    self.drain_instance(id);
                }
            } else {
                instance.desired = Desired::Removed;
                self.drain_instance(id);
            }
            affected.push(id);
        }
        for (plugin, manifest) in desired {
            if self
                .scope(host)?
                .instances
                .get(&plugin)
                .is_some_and(|id| self.instances[id].phase != Phase::Disposed)
            {
                continue;
            }
            let id = self.allocate()?;
            self.instances.insert(
                id,
                Instance {
                    id,
                    host,
                    manifest,
                    next_manifest: None,
                    generation: 1,
                    desired: Desired::Enabled,
                    phase: Phase::Pending,
                    bindings: BTreeMap::new(),
                    registrations: BTreeMap::new(),
                    resources: Vec::new(),
                    init: None,
                    cleanup: None,
                    failed_init: false,
                    failure: None,
                },
            );
            self.scopes
                .get_mut(&host)
                .unwrap()
                .instances
                .insert(plugin, id);
            affected.push(id);
        }
        let affected = self.affected_closure(&affected);
        let scope = self.scopes.get_mut(&host).unwrap();
        scope.order = order;
        if !affected.is_empty() {
            scope.desired_revision += 1;
        }
        if !affected.is_empty() {
            scope.affected = affected;
        }
        self.prune_retired();
        self.reconcile()?;
        self.inspect(host)
    }

    fn affected_closure(&self, initial: &[u64]) -> Vec<u64> {
        let mut affected: BTreeSet<_> = initial.iter().copied().collect();
        loop {
            let before = affected.len();
            for instance in self.instances.values() {
                if instance
                    .bindings
                    .values()
                    .any(|binding| affected.contains(&binding.instance))
                {
                    affected.insert(instance.id);
                }
            }
            if before == affected.len() {
                break;
            }
        }
        affected.into_iter().collect()
    }

    fn counts(&self, runtime: Option<u64>) -> Value {
        let hosts: BTreeSet<_> = self
            .scopes
            .iter()
            .filter(|(_, scope)| runtime.is_none_or(|runtime| scope.runtime == runtime))
            .map(|(host, _)| *host)
            .collect();
        let instances: Vec<_> = self
            .instances
            .values()
            .filter(|instance| hosts.contains(&instance.host))
            .collect();
        let operations: Vec<_> = self
            .operations
            .values()
            .filter(|operation| runtime.is_none_or(|runtime| operation.action.runtime == runtime))
            .collect();
        json!({"scopes":hosts.len(),"instances":instances.len(),"operations":operations.len(),
            "pending_operations":operations.iter().filter(|operation| operation.completion.is_none()).count(),
            "resources":instances.iter().flat_map(|instance| &instance.resources).filter(|resource| !resource.done).count(),
            "listeners":instances.iter().flat_map(|instance| &instance.resources).filter(|resource| !resource.done && resource.event.is_some()).count(),
            "leases":self.leases.values().filter(|lease| hosts.contains(&lease.host)).count(),
            "queued_actions":self.actions.iter().filter(|action| hosts.contains(&action.host)).count()})
    }

    fn ack(&mut self, host: u64, operations: Vec<u64>) -> Result<Value, Error> {
        self.scope(host)?;
        if operations.len() > 1024 {
            return Err(Error::new(
                "invalid_limit",
                "ack accepts at most 1024 operations",
            ));
        }
        let operations: BTreeSet<_> = operations.into_iter().collect();
        for id in &operations {
            if let Some(operation) = self.operations.get(id) {
                if operation.action.host != host {
                    return Err(Error::new(
                        "operation_scope",
                        "operation belongs to another scope",
                    ));
                }
                if operation.completion.is_none() {
                    return Err(Error::new(
                        "operation_pending",
                        "only terminal operations may be acknowledged",
                    ));
                }
            }
        }
        self.operations.retain(|id, _| !operations.contains(id));
        self.actions
            .retain(|action| !operations.contains(&action.operation));
        self.prune_retired();
        Ok(json!({"acknowledged":operations.len()}))
    }

    fn prune_retired(&mut self) {
        if self.retired.is_empty() {
            return;
        }
        let retained: BTreeSet<_> =
            self.operations
                .values()
                .map(|operation| operation.action.instance)
                .chain(self.actions.iter().map(|action| action.instance))
                .chain(
                    self.leases
                        .values()
                        .flat_map(|lease| lease.owners.iter().copied()),
                )
                .chain(self.instances.values().flat_map(|instance| {
                    instance.bindings.values().map(|binding| binding.instance)
                }))
                .collect();
        let released: Vec<_> = self
            .retired
            .iter()
            .filter(|id| !retained.contains(*id))
            .copied()
            .collect();
        for id in released {
            self.retired.remove(&id);
            self.instances.remove(&id);
        }
    }

    fn diagnostic(
        &mut self,
        host: u64,
        code: String,
        message: String,
        instance: Option<u64>,
        operation: Option<u64>,
    ) -> Result<Value, Error> {
        let runtime = self.scope(host)?.runtime;
        if !manifest::valid_name(&code) || message.len() > 4096 {
            return Err(Error::new(
                "invalid_diagnostic",
                "diagnostics need a valid code and at most 4096 message bytes",
            ));
        }
        if instance.is_some_and(|instance| {
            self.instances
                .get(&instance)
                .is_none_or(|instance| self.scopes[&instance.host].runtime != runtime)
        }) || operation.is_some_and(|operation| {
            self.operations
                .get(&operation)
                .is_none_or(|operation| operation.action.runtime != runtime)
        }) {
            return Err(Error::new(
                "diagnostic_identity",
                "diagnostic identity belongs to another runtime",
            ));
        }
        if self.diagnostics.len() == 1024 {
            self.diagnostics.remove(0);
        }
        self.diagnostics.push(json!({"host":host,"runtime":runtime,"code":code,"message":message,"instance":instance,"operation":operation}));
        Ok(json!({"recorded":true}))
    }

    fn collect(&mut self, host: u64) -> Result<Value, Error> {
        let snapshot = self.inspect(host)?;
        if self.state(host)? != "disposed" {
            return Err(Error::new(
                "scope_busy",
                "only completely disposed scopes can be collected",
            ));
        }
        let mut hosts = BTreeSet::from([host]);
        loop {
            let size = hosts.len();
            for (id, scope) in &self.scopes {
                if scope.parent.is_some_and(|parent| hosts.contains(&parent)) {
                    hosts.insert(*id);
                }
            }
            if size == hosts.len() {
                break;
            }
        }
        if hosts
            .iter()
            .any(|host| self.state(*host).is_ok_and(|state| state != "disposed"))
            || self
                .leases
                .values()
                .any(|lease| hosts.contains(&lease.host))
            || self.operations.values().any(|operation| {
                hosts.contains(&operation.action.host) && operation.completion.is_none()
            })
        {
            return Err(Error::new(
                "scope_busy",
                "descendants or operations have not terminated",
            ));
        }
        let before = self.counts(None);
        let retired: BTreeSet<_> = self
            .instances
            .values()
            .filter(|instance| hosts.contains(&instance.host))
            .map(|instance| instance.id)
            .collect();
        self.instances.retain(|id, _| !retired.contains(id));
        self.retired.retain(|id| !retired.contains(id));
        self.operations.retain(|_, operation| {
            !retired.contains(&operation.action.instance) && !hosts.contains(&operation.action.host)
        });
        self.actions
            .retain(|action| !retired.contains(&action.instance) && !hosts.contains(&action.host));
        self.scopes.retain(|id, _| !hosts.contains(id));
        for scope in self.scopes.values_mut() {
            scope.children.retain(|id| !hosts.contains(id));
            scope.affected.retain(|id| !retired.contains(id));
        }
        self.diagnostics.retain(|diagnostic| {
            diagnostic["host"]
                .as_u64()
                .is_none_or(|host| !hosts.contains(&host))
        });
        let remaining = self.counts(None);
        Ok(
            json!({"snapshot":snapshot,"collected":{"scopes":hosts.len(),"instances":retired.len(),"operations":before["operations"].as_u64().unwrap()-remaining["operations"].as_u64().unwrap()},"remaining":remaining}),
        )
    }
}

fn merge(target: &mut Value, overlay: &Value) {
    if let (Some(target), Some(overlay)) = (target.as_object_mut(), overlay.as_object()) {
        for (key, value) in overlay {
            merge(target.entry(key).or_insert(Value::Null), value);
        }
    } else {
        *target = overlay.clone();
    }
}
