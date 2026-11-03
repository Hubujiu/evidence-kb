from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.session_catalog_checks import session_catalog_violations
from . import formal_state_machine_contracts as fixtures
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class SessionCatalogLintContracts(unittest.TestCase):
    def fixture(self, root):
        registry = fixtures.FormalRegistryContracts().fixture(root)
        actual = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        actual = next(machine for machine in actual['machines'] if machine['id'] == 'session_catalog')
        machine = registry['machines'][0]
        machine.update(status='partial', remaining_obligations=['Synthetic semantic lint fixture, not a TLC proof'])
        for channel in ('models', 'negative_controls'):
            machine[channel] = deepcopy(actual[channel])
        paths = {row[key] for row in machine['models'] + machine['negative_controls'] for key in ('module', 'config')}
        paths.add('kb_platform/session_store.py')
        for path in paths:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / path).read_bytes())
        binding = {'path': 'kb_platform/session_store.py', 'language': 'python', 'symbol': '<module>'}
        binding['sha256'] = source_digest(root, binding)
        machine['source_bindings'].append(binding)
        machine['owners'].append(binding['path'])
        return registry

    def test_actual_catalog_source_model_and_required_witnesses_are_accepted(self):
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        self.assertEqual(session_catalog_violations(ROOT, registry), [])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(registry_violations(root, self.fixture(root)), [])

    def test_hash_refreshed_owner_content_commit_and_reachability_mutants_are_rejected(self):
        mutations = {
            'owner-lock-default': ('*, lock=True', '*, lock=False'),
            'tenant': ("self._tables['sessions'].c.tenant == actor.tenant_id", "self._tables['sessions'].c.tenant == self._tables['sessions'].c.tenant"),
            'principal': ("self._tables['sessions'].c.principal == actor.principal_id", "self._tables['sessions'].c.principal == self._tables['sessions'].c.principal"),
            'session-key': ("self._tables['artifacts'].c.session == session_id,", "self._tables['artifacts'].c.session == self._tables['artifacts'].c.session,"),
            'lease': ('            self._lease(db, session_id, token)', '            pass'),
            'integrity-read': ('if digest(body) != artifact_id:', 'if False:'),
            'integrity-dedup': ("elif digest(json.loads(previous['body'])) != key:", 'elif False:'),
            'digest': ('return hashlib.sha256(canonical(value).encode()).hexdigest()', "return 'constant'"),
            'create-publication': ('        return self.load(actor, session_id)', '            return self.load(actor, session_id)'),
            'artifact-publication': ("        return {'artifact_id': key", "            return {'artifact_id': key"),
            'state-payload': ("'state': canonical(state)", "'state': canonical({})"),
        }
        for name in [*mutations, 'old-rhs', 'initial-only', 'missing-witness', 'witness-fault']:
            with self.subTest(mutation=name), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                if name in mutations:
                    path = root / 'kb_platform/session_store.py'
                    before, after = mutations[name]
                elif name == 'old-rhs':
                    path = root / 'formal/session_catalog/SessionCatalog.tla'
                    before, after = '(dedupSeen \\/ stored[key] = "good")', 'dedupSeen \\/ stored[key] = "good"'
                elif name == 'initial-only':
                    path = root / 'formal/session_catalog/SessionCatalog.tla'
                    before, after = 'observed.ok /\\ observed.artifact /\\', 'observed.artifact /\\'
                elif name == 'witness-fault':
                    path = root / 'formal/session_catalog/SessionCatalogAuthorizedReadWitness.cfg'
                    before, after = 'CheckContent = TRUE', 'CheckContent = FALSE'
                else:
                    machine = registry['machines'][0]
                    machine['negative_controls'] = [row for row in machine['negative_controls']
                                                    if row['expected_invariant'] != 'NoCreateUnknownWitness']
                    (root / 'formal/session_catalog/SessionCatalogCreateUnknownWitness.cfg').unlink()
                if name != 'missing-witness':
                    source = path.read_text()
                    self.assertIn(before, source)
                    path.write_text(source.replace(before, after))
                for binding in registry['machines'][0]['source_bindings']:
                    binding['sha256'] = source_digest(root, binding)
                errors = registry_violations(root, registry)
                self.assertTrue(any('session catalog' in error for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)


if __name__ == '__main__':
    unittest.main()
