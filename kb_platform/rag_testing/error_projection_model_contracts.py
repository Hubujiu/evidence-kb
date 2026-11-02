from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.error_projection_model_checks import WITNESSES, error_projection_model_violations
from . import formal_state_machine_contracts as fixtures
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class ErrorProjectionModelContracts(unittest.TestCase):
    def fixture(self, root):
        registry = fixtures.FormalRegistryContracts().fixture(root)
        actual = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        actual = next(machine for machine in actual['machines'] if machine['id'] == 'cache_evidence')
        module = 'formal/cache_evidence/ErrorProjection.tla'
        machine = registry['machines'][0]
        machine['status'] = 'partial'
        machine['remaining_obligations'] = ['Synthetic review guard fixture, not a TLC proof']
        machine['models'] = deepcopy([row for row in actual['models'] if row['module'] == module])
        machine['negative_controls'] = deepcopy([row for row in actual['negative_controls'] if row['module'] == module])
        paths = {module, *(row['config'] for row in machine['models'] + machine['negative_controls'])}
        for path in paths:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / path).read_bytes())
        return registry

    def test_actual_projection_requires_registered_terminal_witnesses(self):
        actual = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        self.assertEqual(error_projection_model_violations(ROOT, actual), [])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(registry_violations(root, self.fixture(root)), [])

    def test_old_rhs_and_weakened_or_unregistered_witnesses_fail_mandatory_lint_after_source_refresh(self):
        for mutation in ('old-rhs', 'initial-only', 'missing-registration', 'missing-witness', 'fault-enabled'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                path = root / 'formal/cache_evidence/ErrorProjection.tla'
                if mutation == 'old-rhs':
                    path.write_text(path.read_text().replace('(LeakGenericText /\\ kind = "generic")',
                                                            'LeakGenericText /\\ kind = "generic"'))
                elif mutation == 'initial-only':
                    path.write_text(path.read_text().replace('NoCodedWitness == ~(' + WITNESSES['Coded'] + ')',
                                                            'NoCodedWitness == ~(kind = "coded")'))
                elif mutation in {'missing-registration', 'missing-witness'}:
                    machine = registry['machines'][0]
                    machine['negative_controls'] = [row for row in machine['negative_controls']
                                                    if row['expected_invariant'] != 'NoCodedWitness']
                    if mutation == 'missing-witness':
                        (root / 'formal/cache_evidence/ErrorProjectionCodedWitness.cfg').unlink()
                else:
                    path = root / 'formal/cache_evidence/ErrorProjectionCodedWitness.cfg'
                    path.write_text(path.read_text().replace('LeakGenericText = FALSE', 'LeakGenericText = TRUE'))
                for binding in registry['machines'][0]['source_bindings']:
                    binding['sha256'] = source_digest(root, binding)
                errors = registry_violations(root, registry)
                self.assertTrue(any('error projection' in error for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)


if __name__ == '__main__':
    unittest.main()
