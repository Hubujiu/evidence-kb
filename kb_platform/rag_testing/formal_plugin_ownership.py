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
    'deadline minimum': ('acquire', '(Some(a), Some(b)) => Some(a.min(b))'),
    'expired admission': ('acquire', 'if deadline.is_some_and(|deadline| deadline <= self.now) { return Err(Error::new("deadline", "deadline already elapsed")); }'),
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
    return errors
