from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.postflight_model_checks import WITNESSES, postflight_model_violations
from . import formal_state_machine_contracts as fixtures
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class PostflightModelContracts(unittest.TestCase):
    def fixture(self, root):
        registry = fixtures.FormalRegistryContracts().fixture(root)
        actual = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        actual = next(machine for machine in actual['machines'] if machine['id'] == 'knowledge_turn_postflight')
        machine = registry['machines'][0]
        machine.update(status='partial', remaining_obligations=['Synthetic model review fixture, not a TLC proof'])
        for channel in ('models', 'negative_controls', 'known_limitations'):
            machine[channel] = deepcopy(actual[channel])
        rows = machine['models'] + machine['negative_controls'] + machine['known_limitations']
        for path in {row[key] for row in rows for key in ('module', 'config')}:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / path).read_bytes())
        return registry

    def test_actual_model_witnesses_and_independent_limitations_are_registered(self):
        actual = json.loads((ROOT / 'formal/registry-v1.json').read_text())
        self.assertEqual(postflight_model_violations(ROOT, actual), [])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(registry_violations(root, self.fixture(root)), [])

    def test_mandatory_lint_rejects_old_rhs_weakened_witnesses_and_hidden_limitations_after_source_refresh(self):
        mutations = ('old-commit', 'old-replay', 'initial-only', 'after-completion-only',
                     'missing-witness', 'limitation-as-mutation', 'missing-old-limitation', 'replay-disabled')
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                machine = registry['machines'][0]
                path = root / 'formal/session_authority/PostflightAuthority.tla'
                if mutation in {'old-commit', 'old-replay'}:
                    rhs = 'scopeAllowed /\\ evidenceAllowed' if mutation == 'old-commit' else 'ValidateReplay /\\ valid'
                    path.write_text(path.read_text().replace('(' + rhs + ')', rhs))
                elif mutation == 'initial-only':
                    path.write_text(path.read_text().replace('NoDeniedReplayWitness == ~(' + WITNESSES['DeniedReplay'] + ')',
                                                            'NoDeniedReplayWitness == ~(mode = "replay")'))
                elif mutation == 'after-completion-only':
                    path.write_text(path.read_text().replace('/\\ ~commitAllowed /\\ commitAuthority = authorityRevision',
                                                            '/\\ commitAuthority = authorityRevision'))
                elif mutation == 'missing-witness':
                    machine['negative_controls'] = [row for row in machine['negative_controls']
                                                    if row['expected_invariant'] != 'NoDeniedReplayWitness']
                    (root / 'formal/session_authority/PostflightAuthorityDeniedReplayWitness.cfg').unlink()
                elif mutation == 'limitation-as-mutation':
                    row = machine['known_limitations'].pop()
                    row['kind'] = 'negative'
                    machine['negative_controls'].append(row)
                elif mutation == 'missing-old-limitation':
                    machine['known_limitations'] = [row for row in machine['known_limitations']
                                                   if row['expected_invariant'] != 'CurrentAuthorityAtCommit']
                    (root / 'formal/session_authority/PostflightAuthorityCommitLimit.cfg').unlink()
                else:
                    path = root / 'formal/session_authority/PostflightAuthorityDeniedReplayWitness.cfg'
                    path.write_text(path.read_text().replace('ValidateReplay = TRUE', 'ValidateReplay = FALSE'))
                for binding in machine['source_bindings']:
                    binding['sha256'] = source_digest(root, binding)
                errors = registry_violations(root, registry)
                self.assertTrue(any('postflight' in error for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)


if __name__ == '__main__':
    unittest.main()
