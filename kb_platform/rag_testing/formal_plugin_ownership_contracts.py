from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from .formal_plugin_ownership import ORDER_RULES, RULES, SOURCE, ownership_violations
from .formal_state_machines import ROOT, registry_violations, source_digest


class PluginOwnershipLintContracts(unittest.TestCase):
    def test_current_guard_shapes_are_reviewed(self):
        self.assertEqual([], ownership_violations(ROOT))

    def test_hash_refresh_cannot_hide_each_scope_guard_mutation(self):
        replacements = {
            'serial ancestry': ('&& !ancestors.contains(id)', '&& ancestors.contains(id)'),
            'runtime identity': ('self.scopes[&instance.host].runtime != scope_runtime', 'self.scopes[&instance.host].runtime == scope_runtime'),
            'scope ancestry': ('if current.is_none()', 'if current.is_some()'),
            'retained owner': ('&& !owners.contains(&owner)', '&& owners.contains(&owner)'),
            'real completion pin': ('&& !self.closing_scope_busy(instance.host)', '&& self.closing_scope_busy(instance.host)'),
            'consumer binding pin': ('binding.instance == id)', 'binding.instance != id)'),
            'unclaimed disposal': ('&& !self.operations[&operation].claimed', '&& self.operations[&operation].claimed'),
            'all dependencies': ('if bindings.len() != instance.manifest.requires.len()', 'if bindings.len() == instance.manifest.requires.len()'),
            'current publication': ('&& current\n', '&& !current\n'),
            'claimed completion': ('if !record.claimed {', 'if record.claimed {'),
            'update generation': ('instance.generation += 1;', 'instance.generation += 0;'),
            'cleanup generation': ('instance.generation += 1;', 'instance.generation += 0;'),
            'reverse resource order': ('.rev()\n                    .find(|resource| !resource.done)', '.find(|resource| !resource.done)'),
            'cleanup error retention': ('instance.phase = Phase::CleanupFailed;', 'instance.phase = Phase::Cleaning;'),
            'resource identity completion': ('Some(resource.id) == action.resource', 'Some(resource.id) != action.resource'),
            'ack owner identity': ('if operation.action.host != host {', 'if operation.action.host == host {'),
            'ack terminal only': ('if operation.completion.is_none() {', 'if operation.completion.is_some() {'),
            'completion duplicate identity': ('if existing == &completion {', 'if existing != &completion {'),
            'retired operation retention': ('operation.action.instance)', 'operation.action.host)'),
            'retired reachability': ('.filter(|id| !retained.contains(*id))', '.filter(|id| retained.contains(*id))'),
            'listener total order': ('(*priority, *scope_order, *resource)', '(*resource, *scope_order, *priority)'),
            'revision update entry': ('self.revision_affected(host, &[id])', 'self.affected_closure(&[id])'),
            'revision apply entry': ('self.revision_affected(host, &affected)', 'self.affected_closure(&affected)'),
            'revision pending carry': ('scope.observed_revision != scope.desired_revision', 'scope.observed_revision == scope.desired_revision'),
            'revision noop identity': ('if initial.is_empty() {', 'if !initial.is_empty() {'),
            'revision transitive closure': ('self.affected_closure(&pending)', 'initial.to_vec()'),
            'admitted binding identity': ('self.registered_binding(host, id, service)', 'self.lookup_binding(host, service, true).unwrap()'),
            'definition binding owner': ('let host = instance.host;', 'let host = call_scope;'),
            'registration binding identity': ('registration: instance.registrations[service]', 'registration: id'),
            'definition intercept chain': ('self.intercept(call_scope, host, service)', 'self.intercept(call_scope, call_scope, service)'),
            'inactive shadow stops': ('if instance.phase != Phase::Active', 'if instance.phase == Phase::Active'),
            'isolation stops ancestor': ('if scope.isolated.contains(service)', 'if !scope.isolated.contains(service)'),
            'intercept precedence': ('for host in chain.into_iter().rev()', 'for host in chain.into_iter()'),
            'borrowed registration visibility': ('binding.registration == registration', 'binding.registration != registration'),
            'collect disposed entry': ('if self.state(host)? != "disposed"', 'if self.state(host)? == "disposed"'),
            'collect stored parent closure': ('scope.parent.is_some_and(|parent| hosts.contains(&parent))', 'scope.parent.is_some_and(|parent| !hosts.contains(&parent))'),
            'collect live lease guard': ('.any(|lease| hosts.contains(&lease.host))', '.any(|lease| !hosts.contains(&lease.host))'),
            'collect pending operation guard': ('hosts.contains(&operation.action.host) && operation.completion.is_none()', 'hosts.contains(&operation.action.host) && operation.completion.is_some()'),
            'collect call host ownership': ('!retired.contains(&operation.action.instance) && !hosts.contains(&operation.action.host)', '!retired.contains(&operation.action.instance)'),
            'collect affected cleanup': ('scope.affected.retain(|id| !retired.contains(id));', 'scope.affected.retain(|id| retired.contains(id));'),
            'collect diagnostic cleanup': ('.is_none_or(|host| !hosts.contains(&host))', '.is_none_or(|host| hosts.contains(&host))'),
            'monotonic handle allocation': ('.checked_add(1)', '.checked_add(0)'),
            'exit scope lookup': ('.get(&host)\n            .and_then(|scope| scope.instances.get(&plugin))', '.values().next()\n            .and_then(|scope| scope.instances.get(&plugin))'),
            'exit instance identity': ('current.id != instance', 'current.id == instance'),
            'exit generation identity': ('current.generation != generation', 'current.generation == generation'),
            'exit enabled guard': ('current.desired != Desired::Enabled', 'current.desired == Desired::Enabled'),
            'exit live phase': ('!matches!(current.phase, Phase::Starting | Phase::Active)', 'matches!(current.phase, Phase::Starting | Phase::Active)'),
            'exit disables target': ('enabled: Some(false)', 'enabled: Some(true)'),
            'diagnostic payload validation': ('message.len() > 4096', 'message.len() > 8192'),
            'diagnostic instance runtime': ('self.scopes[&instance.host].runtime != runtime', 'self.scopes[&instance.host].runtime == runtime'),
            'diagnostic operation runtime': ('operation.action.runtime != runtime', 'operation.action.runtime == runtime'),
            'diagnostic bounded FIFO': ('self.diagnostics.remove(0);', 'self.diagnostics.pop();'),
            'diagnostic runtime projection': ('diagnostic["runtime"] == scope.runtime', 'diagnostic["runtime"] != scope.runtime'),
            'diagnostic validation before eviction': ('if instance.is_some_and(|instance| {', 'if self.diagnostics.len() == 1024 { self.diagnostics.remove(0); } if instance.is_some_and(|instance| {'),
            'failed init explicit terminal': ('_ if instance.failed_init => Phase::Failed', '_ if instance.failed_init => Phase::Pending'),
            'failed init dispatch fence': ('&& !instance.failed_init', '&& instance.failed_init'),
            'retry owner selection': ('plugin.as_ref().is_none_or(|plugin| *name == plugin)', 'plugin.as_ref().is_none_or(|plugin| *name != plugin)'),
            'retry clears failed init': ('instance.failed_init = false;', 'instance.failed_init = true;'),
            'retry new generation': ('instance.generation += 1;', 'instance.generation += 0;'),
            'init authority identity': ('&& instance.init == Some(operation)', '&& instance.init != Some(operation)'),
            'init authority claim': ('record.claimed\n                            && record.completion.is_none()', '!record.claimed\n                            && record.completion.is_none()'),
            'inherited init closure': ('.is_none_or(|record| record.completion.is_some())', '.is_none_or(|record| record.completion.is_none())'),
            'consumer generation identity': ('consumer_generation != Some(instance.generation)', 'consumer_generation == Some(instance.generation)'),
            'consumer declared dependency': ('if instance.host != host', 'if instance.host == host'),
            'current cleanup authority': ('instance.cleanup != Some(operation)', 'instance.cleanup == Some(operation)'),
            'consumer live lookup': ('!matches!(instance.phase, Phase::Starting | Phase::Active)', 'matches!(instance.phase, Phase::Starting | Phase::Active)'),
            'resource operation authority': ('|| !record.claimed', '|| record.claimed'),
            'poll scoped owner': ('hosts.contains(&action.host) && actions.len() < limit', 'hosts.contains(&action.host) || actions.len() < limit'),
            'poll bounded batch': ('if limit == 0 || limit > 1024', 'if limit > 1024'),
            'poll queue FIFO': ('self.actions.pop_front()', 'self.actions.pop_back()'),
            'poll unselected preservation': ('rest.push_back(action);', 'rest.push_front(action);'),
            'poll claims normal action': ('record.claimed = true;', 'record.claimed = false;'),
            'poll cancellation snapshot': ('action.cancelled = record.action.cancelled;', 'action.cancelled = false;'),
            'poll publishes remaining queue': ('self.actions = rest;', 'self.actions.clear();'),
            'deadline minimum': ('Some(a.min(b))', 'Some(a.max(b))'),
            'expired admission': ('deadline <= self.now', 'deadline < self.now'),
        }
        self.assertEqual(set(RULES) | set(ORDER_RULES), set(replacements))
        registered = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        baseline = {'schema_version': 1, 'discovery': {'roots': ['rust/plugin-runtime'], 'exclusions': []},
                    'machines': [next(machine for machine in registered['machines']
                                      if machine['id'] == 'plugin_lifecycle')]}
        for label, (before, after) in replacements.items():
            with self.subTest(rule=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for path in ('formal/controls', 'formal/plugin_lifecycle', 'formal/plugin_ownership', 'rust/plugin-runtime'):
                    shutil.copytree(ROOT / path, root / path, ignore=shutil.ignore_patterns('__pycache__'))
                target = root / SOURCE
                source = target.read_text('utf-8')
                self.assertIn(before, source)
                target.write_text(source.replace(before, after), encoding='utf-8')
                registry = deepcopy(baseline)
                for machine in registry['machines']:
                    for binding in machine['source_bindings']:
                        if binding['path'] == SOURCE:
                            binding['sha256'] = source_digest(root, binding)
                errors = registry_violations(root, registry)
                self.assertFalse(any('source review drift' in error for error in errors), errors)
                self.assertTrue(any(label in error for error in errors), errors)
