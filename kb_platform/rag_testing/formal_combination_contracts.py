import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from scripts.check_project import exact_history_fingerprint_violations, offline_bootstrap_violations
from .formal_state_machines import source_digest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'formal/combination-inputs-embedding-v7.json'


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

    def test_active_inputs_require_exact_registered_definitions_not_counts(self):
        self.assertEqual(self.manifest['schema_version'], 2)
        identities = [row['pr'] for row in self.manifest['inputs']]
        self.assertEqual(len(identities), len(set(identities)))
        for row in self.manifest['inputs']:
            for key in ('head', 'tree', 'base', 'base_tree'):
                self.assertRegex(row[key], r'^[0-9a-f]{40}$')
        self.assertEqual(run_definition_violations(self.registry, self.manifest['required_runs']), [])

    def test_linux_scope_preserves_historical_owner_obligations(self):
        linux = json.loads((ROOT / 'formal/combination-inputs-linux-v2.json').read_text('utf-8'))
        historical_bytes = (ROOT / 'formal/combination-inputs-v1.json').read_bytes()
        self.assertEqual(model_input_digest(historical_bytes),
                         '2cff8fd697800b6feb7b85b3f9189d0a876dc3e911f2d65b06a7df5593477636')
        historical = json.loads(historical_bytes)
        self.assertEqual(
            [row for row in historical['required_runs'] if row['machine'] != 'check_process_owner'],
            [row for row in linux['required_runs'] if row['machine'] != 'check_process_owner'])
        self.assertEqual(
            [row for row in historical['model_inputs'] if not row['path'].startswith('formal/check_process_owner/')],
            [row for row in linux['model_inputs'] if not row['path'].startswith('formal/check_process_owner/')])
        for path, cases in historical['required_contract_cases'].items():
            if path not in ('kb_platform/rag_testing/check_process_owner_windows_contracts.py',
                            'kb_platform/rag_testing/formal_combination_contracts.py'):
                self.assertTrue(set(cases) <= set(linux['required_contract_cases'][path]), path)
        combination_path = 'kb_platform/rag_testing/formal_combination_contracts.py'
        renamed = {
            'test_frozen_public_inputs_require_exact_registered_definitions_not_counts':
                'test_active_inputs_require_exact_registered_definitions_not_counts',
            'test_required_tla_and_cfg_inputs_are_exact_reviewed_public_bytes':
                'test_required_tla_and_cfg_inputs_are_exact_reviewed_active_bytes',
        }
        old_combination = set(historical['required_contract_cases'][combination_path])
        for old, new in renamed.items():
            identity = f'FormalCombinationContracts.{old}'
            self.assertIn(identity, old_combination)
            old_combination.remove(identity)
            old_combination.add(f'FormalCombinationContracts.{new}')
        self.assertTrue(old_combination <= set(linux['required_contract_cases'][combination_path]))
        retired = {'check_process_owner_windows', 'check_process_owner_coverage_pass_windows'}
        old_runs = {row['id']: row for row in historical['required_runs']
                    if row['machine'] == 'check_process_owner'}
        new_runs = {row['id']: row for row in linux['required_runs']
                    if row['machine'] == 'check_process_owner'}
        self.assertEqual(new_runs, {name: row for name, row in old_runs.items() if name not in retired})
        old_models = {row['path']: row for row in historical['model_inputs']
                      if row['path'].startswith('formal/check_process_owner/')}
        new_models = {row['path']: row for row in linux['model_inputs']
                      if row['path'].startswith('formal/check_process_owner/')}
        removed = {'formal/check_process_owner/CheckProcessOwnerWindows.cfg',
                   'formal/check_process_owner/ReachPassWindows.cfg'}
        changed = {'formal/check_process_owner/CheckProcessOwner.tla',
                   'formal/check_process_owner/EarlyResumeSeeded.cfg'}
        self.assertEqual(set(new_models), set(old_models) - removed)
        for path in set(new_models) - changed:
            self.assertEqual(new_models[path], old_models[path])
        path = 'kb_platform/rag_testing/check_process_owner_contracts.py'
        self.assertTrue(set(historical['required_contract_cases'][path]) <=
                        set(linux['required_contract_cases'][path]))
        self.assertIn('CheckProcessOwnerContracts.test_non_linux_platform_refuses_both_entrypoints_before_spawn',
                      linux['required_contract_cases'][path])
        self.assertNotIn('kb_platform/rag_testing/check_process_owner_windows_contracts.py',
                         linux['required_contract_cases'])

    def test_removed_positive_negative_or_known_limitation_cannot_hide_in_same_size_inventory(self):
        for field in ('models', 'negative_controls', 'known_limitations'):
            changed = deepcopy(self.registry)
            expected = self.manifest['required_runs']
            if not any(machine.get(field) for machine in changed['machines']):
                row = {'id': 'synthetic-retained-limitation', 'expected_invariant': 'SyntheticSafety'}
                changed['machines'].append({'id': 'synthetic', field: [row]})
                expected = [*expected, {'machine': 'synthetic', 'field': field, 'id': row['id'],
                    'definition_sha256': definition_digest(row)}]
            owner = next(machine for machine in changed['machines'] if machine.get(field))
            original = owner[field][0]['id']
            owner[field][0]['id'] = 'unreviewed-replacement'
            with self.subTest(field=field, original=original):
                errors = run_definition_violations(changed, expected)
                self.assertTrue(any(original in error for error in errors), errors)

    def test_authority_revision_preserves_unrelated_linux_obligations_and_records_each_change(self):
        previous = json.loads((ROOT / 'formal/combination-inputs-linux-v2.json').read_text('utf-8'))
        authority = json.loads((ROOT / 'formal/combination-inputs-authority-v3.json').read_text('utf-8'))
        changes = authority['reviewed_changes_from_linux_v2']
        for field in ('required_runs', 'model_inputs', 'shared_entry_points'):
            rows = deepcopy(previous[field])
            change = changes[field]
            for row in change['removed']:
                rows.remove(row)
            for pair in change['changed']:
                rows.remove(pair['before'])
                rows.append(pair['after'])
            rows.extend(change['added'])
            self.assertCountEqual(rows, authority[field])
        runs = changes['required_runs']
        historical = {'knowledge_authority_commit_limitation', 'knowledge_postflight_witness_evidencerevokedcommit'}
        self.assertEqual({row['id'] for row in runs['removed']}, historical)
        self.assertTrue(all(row['field'] == 'known_limitations' for row in runs['removed']))
        self.assertEqual({row['id'] for row in runs['added'] if row['machine'] == 'knowledge_turn_postflight'}, historical)
        self.assertEqual({row['after']['id'] for row in runs['changed']}, {'knowledge_postflight_authority'})
        self.assertTrue(all(row['machine'] in {'knowledge_turn_postflight', 'knowledge_atomic_commit'}
            for row in runs['added']))
        models = changes['model_inputs']
        self.assertEqual(models['removed'], [])
        self.assertTrue(all(row['path'].startswith('formal/session_authority/AtomicAuthorityCommit')
            for row in models['added']))
        self.assertTrue(all(row['after']['path'].startswith('formal/session_authority/PostflightAuthority')
            for row in models['changed']))
        for path, cases in previous['required_contract_cases'].items():
            self.assertTrue(set(cases) <= set(authority['required_contract_cases'][path]))

    def test_composition_revision_preserves_authority_evidence_and_records_each_change(self):
        previous = json.loads((ROOT / 'formal/combination-inputs-authority-v3.json').read_text('utf-8'))
        composition = json.loads((ROOT / 'formal/combination-inputs-composition-v4.json').read_text('utf-8'))
        changes = composition['reviewed_changes_from_authority_v3']
        for field in ('required_runs', 'model_inputs', 'shared_entry_points'):
            rows = deepcopy(previous[field])
            change = changes[field]
            self.assertEqual(change['removed'], [])
            for pair in change['changed']:
                rows.remove(pair['before'])
                rows.append(pair['after'])
            rows.extend(change['added'])
            self.assertCountEqual(rows, composition[field])
        self.assertEqual(changes['required_runs']['changed'], [])
        self.assertEqual(changes['model_inputs']['changed'], [])
        for path, cases in previous['required_contract_cases'].items():
            self.assertTrue(set(cases) <= set(composition['required_contract_cases'][path]))

    def test_product_revision_retains_all_composition_obligations_and_records_exact_changes(self):
        previous = json.loads((ROOT / 'formal/combination-inputs-composition-v4.json').read_text('utf-8'))
        product = json.loads((ROOT / 'formal/combination-inputs-product-v5.json').read_text('utf-8'))
        changes = product['reviewed_changes_from_composition_v4']
        for field in ('required_runs', 'model_inputs', 'shared_entry_points'):
            rows = deepcopy(previous[field])
            change = changes[field]
            self.assertEqual(change['removed'], [])
            for pair in change['changed']:
                rows.remove(pair['before'])
                rows.append(pair['after'])
            rows.extend(change['added'])
            self.assertCountEqual(rows, product[field])
        self.assertEqual({row['after']['id'] for row in changes['required_runs']['changed']},
                         {'knowledge-workflow'})
        self.assertEqual({row['id'] for row in changes['required_runs']['added']}, {
            'knowledge-workflow-unsafe-input-correction', 'knowledge-workflow-repeated-input-correction',
            'knowledge-workflow-input-correction-witness'})
        self.assertTrue(all(row['after']['path'].startswith('formal/knowledge_workflow/')
                            for row in changes['model_inputs']['changed']))
        for path, cases in previous['required_contract_cases'].items():
            self.assertTrue(set(cases) <= set(product['required_contract_cases'][path]), path)
        api = json.loads((ROOT / 'formal/combination-inputs-api-v6.json').read_text('utf-8'))
        changes = api['reviewed_changes_from_product_v5']
        for field in ('required_runs', 'model_inputs', 'shared_entry_points'):
            change = changes[field]
            self.assertEqual(change['removed'], [])
            self.assertEqual(change['changed'], [])
            self.assertCountEqual([*product[field], *change['added']], api[field])
        self.assertEqual({row['id'] for row in changes['required_runs']['added']}, {
            'batch_recovery', 'batch_recovery_mutation', 'batch_recovery_authorization'})
        self.assertEqual({row['path'] for row in changes['model_inputs']['added']}, {
            'formal/budget_batches/BatchRecovery.tla', 'formal/budget_batches/BatchRecovery.cfg',
            'formal/budget_batches/BatchRecoveryMutationNegative.cfg',
            'formal/budget_batches/BatchRecoveryAuthorizationNegative.cfg'})
        for path, cases in product['required_contract_cases'].items():
            self.assertTrue(set(cases) <= set(api['required_contract_cases'][path]), path)

    def test_embedding_revision_retains_api_obligations_and_adds_source_view_slice(self):
        previous = json.loads((ROOT / 'formal/combination-inputs-api-v6.json').read_text('utf-8'))
        changes = self.manifest['reviewed_changes_from_api_v6']
        for field in ('required_runs', 'model_inputs', 'shared_entry_points'):
            change = changes[field]
            self.assertEqual(change['removed'], [])
            self.assertEqual(change['changed'], [])
            self.assertCountEqual([*previous[field], *change['added']], self.manifest[field])
        self.assertEqual({row['id'] for row in changes['required_runs']['added']}, {
            'embedding-source-view', 'embedding-source-view-candidate', 'embedding-source-view-frozen-policy',
            'embedding-source-view-published-witness', 'embedding-source-view-candidate-witness'})
        self.assertEqual({row['path'] for row in changes['model_inputs']['added']}, {
            'formal/authority_publication/EmbeddingSourceView.tla',
            'formal/authority_publication/EmbeddingSourceView.cfg',
            'formal/authority_publication/EmbeddingSourceViewCandidate.cfg',
            'formal/authority_publication/EmbeddingSourceViewFrozenPolicy.cfg',
            'formal/authority_publication/EmbeddingSourceViewPublishedWitness.cfg',
            'formal/authority_publication/EmbeddingSourceViewCandidateWitness.cfg'})
        for path, cases in previous['required_contract_cases'].items():
            self.assertTrue(set(cases) <= set(self.manifest['required_contract_cases'][path]), path)

    def test_changed_control_invariant_is_rejected_without_changing_any_id(self):
        changed = deepcopy(self.registry)
        owner = next(machine for machine in changed['machines'] if machine['negative_controls'])
        control = owner['negative_controls'][0]
        control['expected_invariant'] = 'UnreviewedInvariant'
        self.assertTrue(run_definition_violations(changed, self.manifest['required_runs']))

    def test_required_tla_and_cfg_inputs_are_exact_reviewed_active_bytes(self):
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
                ("errors.extend(f'{identity}: {error}' for error in check_process_owner_violations(root))", 'pass'),
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

    def test_delivery_control_cases_remain_mandatory(self):
        path = ROOT / 'kb_platform/rag_testing/foundation_integration.py'
        source = path.read_text('utf-8')

        def has_delivery(source_text):
            tree = ast.parse(source_text)
            suite = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == 'integration_suite')
            loops = [node for node in ast.walk(suite) if isinstance(node, ast.For)
                     and isinstance(node.iter, ast.Tuple)
                     and sum(isinstance(item, ast.Name) and item.id == 'delivery_control_integration'
                             for item in node.iter.elts) == 1]
            expected = ast.parse('suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))').body[0]
            return any(any(ast.dump(item) == ast.dump(expected) for item in loop.body) for loop in loops)

        self.assertTrue(has_delivery(source))
        original = '                   delivery_control_integration,\n'
        self.assertEqual(source.count(original), 1)
        self.assertFalse(has_delivery(source.replace(original, '', 1)))

    def test_check_and_worker_bindings_detect_removed_ownership_and_offline_seal(self):
        path = 'scripts/check_project.py'
        symbols = ('run_check', 'run_worker')
        bindings = [next(row for row in self.manifest['shared_entry_points']
                         if row['path'] == path and row['symbol'] == symbol) for symbol in symbols]
        original = (ROOT / path).read_text('utf-8')
        mutations = (
            ('run_check', 'result = run_owned(command, cwd=ROOT, env=env, stream=stream, timeout=timeout)',
             'result = None'),
            ('run_worker', '    machine = seal_offline_worker()\n    observation =',
             '    machine = None\n    observation ='),
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
                    changed = original.replace(before, after, 1)
                    target.write_text(changed, encoding='utf-8')
                    self.assertEqual(shared_entry_point_violations(root, bindings),
                                     [f'Shared entry point changed: {path}:{symbol}'])
                    if symbol == 'run_worker':
                        self.assertTrue(offline_bootstrap_violations(ast.parse(changed)))

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
