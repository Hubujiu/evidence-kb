import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from scripts.check_project import exact_history_fingerprint_violations
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


def model_input_violations(root, registry, expected):
    if not isinstance(expected, list):
        return ['Model inputs require explicit path/sha256 records']
    required = {row[key] for machine in registry['machines']
                for field in ('models', 'negative_controls', 'known_limitations')
                for row in machine.get(field, []) for key in ('module', 'config')}
    found = {}
    errors = []
    for row in expected:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256'}:
            errors.append('Model input record requires exactly path and sha256')
            continue
        path, digest = row['path'], row['sha256']
        if not isinstance(path, str) or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            errors.append('Model input record has invalid path or digest')
            continue
        if path in found:
            errors.append(f'Duplicate model input: {path}')
        found[path] = digest
    errors.extend(f'Missing model input: {path}' for path in sorted(required - found.keys()))
    errors.extend(f'Unexpected model input: {path}' for path in sorted(found.keys() - required))
    for path in sorted(required & found.keys()):
        try:
            actual = model_input_digest((root / path).read_bytes())
        except OSError:
            errors.append(f'Model input is unavailable: {path}')
            continue
        if actual != found[path]:
            errors.append(f'Model input bytes changed: {path}')
    return errors


def shared_entry_point_violations(root, expected):
    errors = []
    for row in expected:
        try:
            actual = source_digest(root, row)
        except (OSError, ValueError):
            actual = None
        if actual != row['sha256']:
            errors.append(f"Shared entry point changed: {row['path']}:{row['symbol']}")
    return errors


class FormalCombinationContracts(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text('utf-8'))
        self.registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))

    def test_frozen_public_inputs_require_exact_registered_definitions_not_counts(self):
        self.assertEqual(self.manifest['schema_version'], 2)
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
        self.assertEqual(model_input_violations(ROOT, self.registry, self.manifest['model_inputs']), [])

    def test_model_input_inventory_rejects_shape_identity_duplicates_and_changed_bytes(self):
        registry = {'machines': [{'models': [{'module': 'example.tla', 'config': 'example.cfg'}]}]}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for path in ('example.tla', 'example.cfg'):
                data = path.encode()
                (root / path).write_bytes(data)
                inputs.append({'path': path, 'sha256': model_input_digest(data)})
            self.assertEqual(model_input_violations(root, registry, inputs), [])
            mutations = (
                {row['path']: row['sha256'] for row in inputs},
                inputs[:-1],
                inputs + [inputs[0]],
                [inputs[0], inputs[0]],
                [inputs[0], {'path': 'unreviewed.cfg', 'sha256': inputs[1]['sha256']}],
                inputs + [{'path': '../unreviewed.cfg', 'sha256': inputs[0]['sha256']}],
                [inputs[0], {'path': inputs[1]['path'], 'sha256': '0' * 64}],
                [inputs[0], {'path': inputs[1]['path']}],
                [inputs[0], {'path': inputs[1]['path'], 'sha256': inputs[1]['sha256'], 'other': 1}],
                [inputs[0], {'path': [], 'sha256': inputs[1]['sha256']}],
                [inputs[0], {'path': inputs[1]['path'], 'sha256': 'not-a-digest'}],
            )
            for index, changed in enumerate(mutations):
                with self.subTest(mutation=index):
                    self.assertTrue(model_input_violations(root, registry, changed))
            (root / 'example.cfg').unlink()
            self.assertTrue(model_input_violations(root, registry, inputs))

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
        self.assertEqual(shared_entry_point_violations(ROOT, self.manifest['shared_entry_points']), [])

    def test_registry_wrapper_cannot_hide_removed_worker_hook_or_binding_validation(self):
        path = 'kb_platform/rag_testing/formal_state_machines.py'
        bindings = [next(row for row in self.manifest['shared_entry_points']
                         if row['path'] == path and row['symbol'] == symbol)
                    for symbol in ('registry_violations', '_registry_violations')]
        original = (ROOT / path).read_text('utf-8')
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir(parents=True)
            target.write_text(original, encoding='utf-8')
            self.assertEqual(shared_entry_point_violations(root, bindings), [])
            for before, after in (
                ('errors.extend(storage_grant_violations(root))', 'pass'),
                ('actual = reviewed_digest(root, binding)', "actual = binding['sha256']"),
            ):
                with self.subTest(mutation=after):
                    self.assertEqual(original.count(before), 1)
                    target.write_text(original.replace(before, after, 1), encoding='utf-8')
                    self.assertEqual(source_digest(root, bindings[0]), bindings[0]['sha256'])
                    self.assertEqual(shared_entry_point_violations(root, bindings),
                                     [f'Shared entry point changed: {path}:_registry_violations'])

    def test_discovery_wrapper_cannot_hide_candidate_body_cache_or_payload_mutation(self):
        path = 'kb_platform/rag_testing/formal_state_machines.py'
        symbols = ('state_candidates', '_OversizedCandidates', '_candidate_symbols_uncached',
                   '_candidate_payload_bytes', '_cached_candidate_symbols', 'candidate_symbols')
        bindings = [next(row for row in self.manifest['shared_entry_points']
                         if row['path'] == path and row['symbol'] == symbol) for symbol in symbols]
        original = (ROOT / path).read_text('utf-8')
        mutations = (
            ('_OversizedCandidates', 'self.result = result', 'self.result = frozenset()'),
            ('_candidate_symbols_uncached', 'if name in state_names:', 'if False:'),
            ('_candidate_payload_bytes', 'total += sys.getsizeof(value)', 'total += 0'),
            ('_cached_candidate_symbols', '@lru_cache(maxsize=512)', '@lru_cache(maxsize=513)'),
            ('candidate_symbols', 'return _cached_candidate_symbols(source, state_names)',
             'return frozenset()'),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir(parents=True)
            target.write_text(original, encoding='utf-8')
            self.assertEqual(shared_entry_point_violations(root, bindings), [])
            for symbol, before, after in mutations:
                with self.subTest(symbol=symbol):
                    self.assertEqual(original.count(before), 1)
                    target.write_text(original.replace(before, after, 1), encoding='utf-8')
                    self.assertEqual(source_digest(root, bindings[0]), bindings[0]['sha256'])
                    self.assertEqual(shared_entry_point_violations(root, bindings),
                                     [f'Shared entry point changed: {path}:{symbol}'])

    def test_historical_fingerprint_scope_cannot_expand_to_other_commits_files_rules_or_lines(self):
        original = (ROOT / '.gitleaksignore').read_text('utf-8').splitlines()
        self.assertEqual(len(original), 11)
        self.assertEqual(exact_history_fingerprint_violations(ROOT), [])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'formal').mkdir()
            (root / 'formal/combination-inputs-v1.json').write_text('{}', encoding='utf-8')
            target = root / '.gitleaksignore'
            self.assertTrue(exact_history_fingerprint_violations(root))
            mutations = [original[:-1], original + [original[0]], ['formal/combination-inputs-v1.json'], ['*']]
            for index, changed in enumerate(('0' * 40, 'other.json', 'other-rule', '9999')):
                parts = original[0].split(':')
                parts[index] = changed
                mutations.append([':'.join(parts)] + original[1:])
            for index, rows in enumerate(mutations):
                with self.subTest(mutation=index):
                    target.write_text('\n'.join(rows) + '\n', encoding='utf-8')
                    self.assertTrue(exact_history_fingerprint_violations(root))
            target.write_text('\n'.join(original) + '\n', encoding='utf-8')
            self.assertEqual(exact_history_fingerprint_violations(root), [])
