from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.durable_session_checks import durable_session_violations
from . import formal_state_machine_contracts as fixtures
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class DurableSessionLintContracts(unittest.TestCase):
    def fixture(self, root):
        registry = fixtures.FormalRegistryContracts().fixture(root)
        actual = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        actual = next(machine for machine in actual['machines'] if machine['id'] == 'session_durable_adapter')
        machine = registry['machines'][0]
        machine.update(status='partial', remaining_obligations=['Synthetic semantic lint fixture, not a TLC proof'])
        for channel in ('models', 'negative_controls'):
            machine[channel] = deepcopy(actual[channel])
        paths = {row[key] for row in machine['models'] + machine['negative_controls'] for key in ('module', 'config')}
        paths.add('kb_platform/harness_sessions.py')
        for path in paths:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / path).read_bytes())
        binding = {'path': 'kb_platform/harness_sessions.py', 'language': 'python', 'symbol': '<module>'}
        binding['sha256'] = source_digest(root, binding)
        machine['source_bindings'].append(binding)
        machine['owners'].append(binding['path'])
        return registry

    def test_actual_adapter_source_and_terminal_witnesses_are_accepted(self):
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        self.assertEqual(durable_session_violations(ROOT, registry), [])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(registry_violations(root, self.fixture(root)), [])

    def test_hash_refreshed_adapter_and_witness_regressions_are_rejected(self):
        mutations = {
            'namespace': ("session['state'].get('namespace') != GENERIC_SESSION", 'False'),
            'unknown-before-begin': ('if _pending_tools(events) and context.turn_id not in settled:', 'if False:'),
            'unknown-after-begin': ('if _pending_tools(events):', 'if False:'),
            'tool-pairing': ("if pending.pop(group, None) != record.get('name'):", 'if False:'),
            'cancellation-check': ('        _check_execution()', '        pass'),
            'copy-record': ('copied = deepcopy(record)', 'copied = record'),
            'copy-cached': ("return deepcopy(self.start_result['cached'])", "return self.start_result['cached']"),
            'copy-messages': ('return deepcopy(self._records)', 'return self._records'),
            'early-close': ('        response = _persist(self.store.finish,', '        self._closed = True\n        response = _persist(self.store.finish,'),
            'terminal-guard': ('if self._closed or self._token is None:', 'if self._token is None:'),
            'turn-number': ("self._state['turn_number'] + 1", "self._state['turn_number'] + 2"),
            'wrap-cancellation': ('except Exception as error:', 'except BaseException as error:'),
            'initialization-cleanup': ('except BaseException as error:', 'except Exception as error:'),
        }
        for name in [*mutations, 'commit-witness', 'replay-witness', 'missing-witness', 'fault-witness']:
            with self.subTest(mutation=name), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                if name in mutations:
                    path = root / 'kb_platform/harness_sessions.py'
                    before, after = mutations[name]
                elif name == 'commit-witness':
                    path = root / 'formal/session_catalog/DurableAdapter.tla'
                    before, after = 's.phase = "uncertain" /\\ s.status = "complete"', 's.status = "complete"'
                elif name == 'replay-witness':
                    path = root / 'formal/session_catalog/DurableAdapter.tla'
                    before, after = 's.phase = "closed" /\\ s.mode = "replay" /\\ s.public = "success"', 's.mode = "replay"'
                elif name == 'fault-witness':
                    path = root / 'formal/session_catalog/DurableAdapterReplayWitness.cfg'
                    before, after = 'ReplayWithoutEffects = TRUE', 'ReplayWithoutEffects = FALSE'
                else:
                    machine = registry['machines'][0]
                    machine['negative_controls'] = [row for row in machine['negative_controls']
                                                    if row['expected_invariant'] != 'NoToolRecordUnknownWitness']
                    (root / 'formal/session_catalog/DurableAdapterToolRecordUnknownWitness.cfg').unlink()
                if name != 'missing-witness':
                    source = path.read_text()
                    self.assertIn(before, source)
                    path.write_text(source.replace(before, after))
                for binding in registry['machines'][0]['source_bindings']:
                    binding['sha256'] = source_digest(root, binding)
                errors = registry_violations(root, registry)
                self.assertTrue(any('durable session' in error for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)


if __name__ == '__main__':
    unittest.main()
