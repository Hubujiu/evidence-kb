from pathlib import Path
import re


SOURCE = 'rust/plugin-runtime/src/coordinator.rs'
RULES = {
    'serial ancestry': ('acquire', 'lease.execution_owner == Some(owner) && !ancestors.contains(id)'),
    'runtime identity': ('acquire', 'if self.scopes[&instance.host].runtime != scope_runtime { return Err(Error::new("lease_scope", "owner belongs to another runtime", )); }'),
    'scope ancestry': ('acquire', 'if current.is_none() { return Err(Error::new("lease_scope", "owner is outside the calling scope ancestry", )); }'),
    'retained owner': ('acquire', 'if instance.phase != Phase::Active && !owners.contains(&owner) && initialization.is_none() { return Err(Error::new("service_inactive", "service no longer accepts calls", )); }'),
    'real completion pin': ('reconcile', '&& !self.leases.values().any(|lease| lease.owners.contains(&instance.id)) && !self.retained_by_consumer(instance.id) && !self.closing_scope_busy(instance.host)'),
    'consumer binding pin': ('retained_by_consumer', 'consumer.bindings.values().any(|binding| binding.instance == id)'),
    'unclaimed disposal': ('cancel_unclaimed_init', 'if let Some(operation) = self.instances[&id].init && !self.operations[&operation].claimed { self.operations.remove(&operation); self.instances.get_mut(&id).unwrap().init = None; self.actions.retain(|action| action.operation != operation); }'),
    'all dependencies': ('reconcile', 'if bindings.len() != instance.manifest.requires.len() { continue; }'),
    'current publication': ('complete', 'let publish = success && current && self.instances[&id].desired == Desired::Enabled'),
    'claimed completion': ('complete', 'if !record.claimed { return Err(Error::new("operation_unclaimed", "poll operation before completing it", )); }'),
    'update generation': ('update', 'instance.phase = Phase::Pending; instance.generation += 1;'),
    'cleanup generation': ('complete', 'if instance.phase == Phase::Pending { instance.generation += 1; instance.failure = None; }'),
    'reverse resource order': ('reconcile', '.resources.iter().rev().find(|resource| !resource.done).map_or(0, |resource| resource.id)'),
    'cleanup error retention': ('complete', 'if !success { instance.phase = Phase::CleanupFailed; instance.failure = completion.error.clone().or_else(|| Some("cleanup failed".into())); } else if action.resource == Some(0)'),
    'resource identity completion': ('complete', '.find(|resource| Some(resource.id) == action.resource) { resource.done = true; }'),
    'ack owner identity': ('ack', 'if operation.action.host != host { return Err(Error::new("operation_scope", "operation belongs to another scope", )); }'),
    'ack terminal only': ('ack', 'if operation.completion.is_none() { return Err(Error::new("operation_pending", "only terminal operations may be acknowledged", )); }'),
    'completion duplicate identity': ('complete', 'if let Some(existing) = &record.completion { if existing == &completion { return Ok(json!({"duplicate":true,"state":self.state(host)?})); }'),
    'retired operation retention': ('prune_retired', 'self.operations.values().map(|operation| operation.action.instance).chain(self.actions.iter().map(|action| action.instance))'),
    'retired reachability': ('prune_retired', '.filter(|id| !retained.contains(*id))'),
    'listener total order': ('listeners', 'listeners.sort_by_key(|(priority, scope_order, resource, _)| { (*priority, *scope_order, *resource) });'),
    'revision update entry': ('update', 'let affected = self.revision_affected(host, &[id]);'),
    'revision apply entry': ('apply', 'let affected = self.revision_affected(host, &affected);'),
    'revision pending carry': ('revision_affected', 'if scope.observed_revision != scope.desired_revision { pending.extend(scope.affected.iter().copied()); }'),
    'revision noop identity': ('revision_affected', 'if initial.is_empty() { return Vec::new(); }'),
    'revision transitive closure': ('revision_affected', 'self.affected_closure(&pending)'),
    'admitted binding identity': ('invoke', 'action.binding = Some(self.registered_binding(host, id, service));'),
    'definition binding owner': ('registered_binding', 'let instance = &self.instances[&id]; let host = instance.host;'),
    'registration binding identity': ('registered_binding', 'registration: instance.registrations[service]'),
    'definition intercept chain': ('registered_binding', 'intercept: self.intercept(call_scope, host, service)'),
    'inactive shadow stops': ('lookup_binding', 'if instance.phase != Phase::Active && !(retained && instance.registrations.contains_key(service)) { return Err(Error::new("service_inactive", format!("{} is not active", instance.manifest.id), )); }'),
    'isolation stops ancestor': ('lookup_binding', 'if scope.isolated.contains(service) { break; }'),
    'intercept precedence': ('intercept', 'for host in chain.into_iter().rev() {'),
    'borrowed registration visibility': ('acquire', '.is_ok_and(|binding| binding.registration == registration)'),
    'collect disposed entry': ('collect', 'if self.state(host)? != "disposed" {'),
    'collect stored parent closure': ('collect', 'if scope.parent.is_some_and(|parent| hosts.contains(&parent)) { hosts.insert(*id); }'),
    'collect live lease guard': ('collect', '.any(|lease| hosts.contains(&lease.host))'),
    'collect pending operation guard': ('collect', 'hosts.contains(&operation.action.host) && operation.completion.is_none()'),
    'collect call host ownership': ('collect', '!retired.contains(&operation.action.instance) && !hosts.contains(&operation.action.host)'),
    'collect affected cleanup': ('collect', 'scope.affected.retain(|id| !retired.contains(id));'),
    'collect diagnostic cleanup': ('collect', '.is_none_or(|host| !hosts.contains(&host))'),
    'monotonic handle allocation': ('allocate', '.checked_add(1)'),
    'exit scope lookup': ('instance_exited', '.get(&host).and_then(|scope| scope.instances.get(&plugin)).and_then(|id| self.instances.get(id))'),
    'exit instance identity': ('instance_exited', 'current.id != instance'),
    'exit generation identity': ('instance_exited', 'current.generation != generation'),
    'exit enabled guard': ('instance_exited', 'current.desired != Desired::Enabled'),
    'exit live phase': ('instance_exited', '!matches!(current.phase, Phase::Starting | Phase::Active)'),
    'exit disables target': ('instance_exited', 'enabled: Some(false)'),
    'diagnostic payload validation': ('diagnostic', '!manifest::valid_name(&code) || message.len() > 4096'),
    'diagnostic instance runtime': ('diagnostic', '.is_none_or(|instance| self.scopes[&instance.host].runtime != runtime)'),
    'diagnostic operation runtime': ('diagnostic', '.is_none_or(|operation| operation.action.runtime != runtime)'),
    'diagnostic bounded FIFO': ('diagnostic', 'if self.diagnostics.len() == 1024 { self.diagnostics.remove(0); }'),
    'diagnostic runtime projection': ('inspect', '.filter(|diagnostic| diagnostic["runtime"] == scope.runtime)'),
    'failed init explicit terminal': ('complete', '_ if instance.failed_init => Phase::Failed'),
    'failed init dispatch fence': ('reconcile', '&& !instance.failed_init && self.open_ancestors(Some(instance.host)).is_ok()'),
    'retry owner selection': ('retry', '.filter(|(name, _)| plugin.as_ref().is_none_or(|plugin| *name == plugin))'),
    'retry clears failed init': ('retry', 'instance.failed_init = false; instance.failure = None;'),
    'retry new generation': ('retry', 'instance.generation += 1;'),
    'init authority identity': ('acquire', 'registration.is_none() && instance.host == host && instance.phase == Phase::Starting && instance.init == Some(operation)'),
    'init authority claim': ('acquire', '.is_some_and(|record| { record.claimed && record.completion.is_none() && record.action.kind == "init"'),
    'inherited init closure': ('acquire', '.is_none_or(|record| record.completion.is_some())'),
    'consumer generation identity': ('resolve', 'consumer_generation != Some(instance.generation)'),
    'consumer declared dependency': ('resolve', 'if instance.host != host || !instance.manifest.requires.iter().any(|name| name == service)'),
    'current cleanup authority': ('resolve', 'action.instance != consumer || action.kind != "cleanup" || instance.cleanup != Some(operation)'),
    'consumer live lookup': ('resolve', '!matches!(instance.phase, Phase::Starting | Phase::Active)'),
    'resource operation authority': ('resource', 'record.completion.is_some() || !record.claimed || self.instances[&record.action.instance].generation != record.action.generation'),
    'poll scoped owner': ('request', 'if hosts.contains(&action.host) && actions.len() < limit'),
    'poll bounded batch': ('request', 'if limit == 0 || limit > 1024'),
    'poll queue FIFO': ('request', 'while let Some(mut action) = self.actions.pop_front()'),
    'poll unselected preservation': ('request', '} else { rest.push_back(action); }'),
    'poll claims normal action': ('request', '&& action.kind != "cancel" { record.claimed = true;'),
    'poll cancellation snapshot': ('request', 'action.cancelled = record.action.cancelled;'),
    'poll publishes remaining queue': ('request', 'self.actions = rest;'),
    'deadline minimum': ('acquire', '(Some(a), Some(b)) => Some(a.min(b))'),
    'expired admission': ('acquire', 'if deadline.is_some_and(|deadline| deadline <= self.now) { return Err(Error::new("deadline", "deadline already elapsed")); }'),
}


ORDER_RULES = {
    'diagnostic validation before eviction': ('diagnostic', 'if instance.is_some_and', 'if self.diagnostics.len() == 1024'),
}


def tokens(source: str) -> list[str]:
    return [m.group() for m in re.finditer(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_][A-Za-z_0-9]*|\d+|[^\s]', source)
            if not m.group().startswith(('//', '/*'))]


def method(source: str, name: str) -> list[str]:
    items = tokens(source)
    for i in range(len(items) - 2):
        if items[i:i + 3] == ['fn', name, '(']:
            start = items.index('{', i)
            depth = 1
            end = start + 1
            while depth and end < len(items):
                depth += (items[end] == '{') - (items[end] == '}')
                end += 1
            return items[start + 1:end - 1] if not depth else []
    return []


def ownership_violations(root: Path) -> list[str]:
    source = (root / SOURCE).read_text('utf-8')
    errors = []
    for label, (name, fragment) in RULES.items():
        body, expected = method(source, name), tokens(fragment)
        if not any(body[i:i + len(expected)] == expected for i in range(len(body) - len(expected) + 1)):
            errors.append(f'plugin ownership: {label} guard requires reviewed shape in {name}')
    for label, (name, first, second) in ORDER_RULES.items():
        body, left, right = method(source, name), tokens(first), tokens(second)
        before = next((i for i in range(len(body)) if body[i:i + len(left)] == left), -1)
        after = next((i for i in range(len(body)) if body[i:i + len(right)] == right), -1)
        if before < 0 or after <= before:
            errors.append(f'plugin ownership: {label} guard requires reviewed order in {name}')
    return errors
