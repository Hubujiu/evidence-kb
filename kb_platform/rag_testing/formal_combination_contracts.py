import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest

from .formal_state_machines import source_digest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'formal/combination-inputs-v1.json'


def model_input_digest(data):
    return sha256(data.replace(b'\r\n', b'\n')).hexdigest()


def definition_digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def run_definition_violations(registry, expected):
    actual = {(machine['id'], field, row['id']): definition_digest(row)
              for machine in registry['machines'] for field in ('models', 'negative_controls', 'known_limitations')
              for row in machine.get(field, [])}
    return [f"Required {row['field']} definition changed: {row['machine']}:{row['id']}"
            for row in expected if actual.get((row['machine'], row['field'], row['id'])) != row['definition_sha256']]


class FormalCombinationContracts(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text('utf-8'))
        self.registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))

    def test_frozen_public_inputs_require_exact_registered_definitions_not_counts(self):
        self.assertEqual(self.manifest['schema_version'], 1)
        identities = [row['pr'] for row in self.manifest['inputs']]
        self.assertEqual(len(identities), len(set(identities)))
        for row in self.manifest['inputs']:
            for key in ('head', 'tree', 'base', 'base_tree'):
                self.assertRegex(row[key], r'^[0-9a-f]{40}$')
        self.assertEqual(run_definition_violations(self.registry, self.manifest['required_runs']), [])

    def test_removed_positive_negative_or_known_limitation_cannot_hide_in_same_size_inventory(self):
        for field in ('models', 'negative_controls', 'known_limitations'):
            changed = deepcopy(self.registry)
            owner = next(machine for machine in changed['machines'] if machine.get(field))
            original = owner[field][0]['id']
            owner[field][0]['id'] = 'unreviewed-replacement'
            with self.subTest(field=field, original=original):
                errors = run_definition_violations(changed, self.manifest['required_runs'])
                self.assertTrue(any(original in error for error in errors), errors)

    def test_changed_control_invariant_is_rejected_without_changing_any_id(self):
        changed = deepcopy(self.registry)
        owner = next(machine for machine in changed['machines'] if machine['negative_controls'])
        control = owner['negative_controls'][0]
        control['expected_invariant'] = 'UnreviewedInvariant'
        self.assertTrue(run_definition_violations(changed, self.manifest['required_runs']))

    def test_required_tla_and_cfg_inputs_are_exact_reviewed_public_bytes(self):
        for path, expected in self.manifest['model_inputs'].items():
            with self.subTest(path=path):
                self.assertEqual(model_input_digest((ROOT / path).read_bytes()), expected)

    def test_model_input_digest_allows_only_checkout_crlf_normalization(self):
        source = b'Init == phase = 0\nNext == UNCHANGED phase\n'
        self.assertEqual(model_input_digest(source), model_input_digest(source.replace(b'\n', b'\r\n')))
        for changed in (source.replace(b'0', b'1'), source.replace(b'\n', b'\r'), source + b' '):
            self.assertNotEqual(model_input_digest(source), model_input_digest(changed))

    def test_public_contract_case_identities_are_retained(self):
        for path, expected in self.manifest['required_contract_cases'].items():
            tree = ast.parse((ROOT / path).read_text('utf-8'))
            actual = {cls.name + '.' + node.name for cls in tree.body if isinstance(cls, ast.ClassDef)
                      for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.name.startswith('test_')}
            with self.subTest(path=path):
                self.assertEqual(set(expected) - actual, set())

    def test_shared_entry_points_keep_the_reviewed_combination_of_mandatory_hooks(self):
        for row in self.manifest['shared_entry_points']:
            with self.subTest(path=row['path'], symbol=row['symbol']):
                self.assertEqual(source_digest(ROOT, row), row['sha256'])
