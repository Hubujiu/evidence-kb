import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.presend_admission_checks import presend_admission_violations

from .formal_state_machines import registry_violations, source_digest


class PresendLintContracts(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parents[1] / 'remote.py').read_text('utf-8')

    def test_current_provider_admission_has_fresh_check_and_known_unsent_exit(self):
        self.assertEqual(presend_admission_violations(self.source), [])

    def test_parseable_mutations_cannot_remove_recheck_or_corrupt_failure_accounting(self):
        mutants = (
            self.source.replace('remaining = remaining_time()', 'remaining = 10.0', 1),
            self.source.replace('            try:\n                remaining = remaining_time()',
                '            self._issued_requests += 1\n            try:\n                remaining = remaining_time()', 1),
            self.source.replace("'model_sent': False, 'preflight_code': preflight.code", "'model_sent': True, 'preflight_code': preflight.code", 1),
            self.source.replace("self._receipts.finish(logical_id, physical_id, 'ERROR', **facts)",
                                "self._receipts.finish(logical_id, physical_id, 'UNKNOWN', **facts)", 1),
            self.source.replace('setattr(storage_error, name, value)\n                    raise',
                                "setattr(storage_error, name, value)\n                    raise preflight", 1),
            self.source.replace('min(self.timeout, remaining) if remaining is not None else self.timeout',
                                'self.timeout', 1),
            self.source.replace("raise RemoteModelError('PROVIDER_CANCELLED')", 'pass', 1),
        )
        for index, source in enumerate(mutants):
            with self.subTest(mutant=index):
                self.assertTrue(presend_admission_violations(source))

    def test_nonsemantic_annotation_and_quotes_do_not_break_admission_lint(self):
        source = self.source.replace('def remaining_time():', 'def remaining_time() -> float | None:')
        source = source.replace("'known_zero_send': True", '"known_zero_send": True')
        self.assertEqual(presend_admission_violations(source), [])

    def registry_fixture(self, root):
        (root / 'kb_platform').mkdir()
        (root / 'formal').mkdir()
        from scripts.physical_settlement_lint import CAPTURE_CONSUMERS, OWNERS

        bindings = []
        paths = ('kb_platform/remote.py', 'kb_platform/model_errors.py', 'kb_platform/model_http.py',
                 'kb_platform/provider_admission.py', *[owner[0] for owner in OWNERS],
                 *[consumer[0] for consumer in CAPTURE_CONSUMERS])
        for path in dict.fromkeys(paths):
            source = (Path(__file__).resolve().parents[2] / path).read_text('utf-8')
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(source, encoding='utf-8')
            binding = {'path': path, 'language': 'python', 'symbol': '<module>'}
            binding['sha256'] = source_digest(root, binding)
            bindings.append(binding)
        return {'schema_version': 1, 'discovery': {'roots': ['kb_platform'], 'exclusions': []},
            'machines': [{'id': 'provider_admission', 'status': 'unmodeled',
                'owners': [binding['path'] for binding in bindings],
                'states': ['STARTED', 'admitted', 'known-unsent'], 'transitions': ['_Transport._send_attempt'],
                'invariants': ['Fresh admission before dispatch'], 'assumptions': ['Synthetic lint fixture'],
                'exclusions': ['Not model coverage'], 'source_bindings': bindings,
                'models': [], 'negative_controls': []}]}

    def test_mandatory_registry_lint_rejects_removed_recheck_after_hash_refresh(self):
        mutants = (
            self.source.replace('remaining = remaining_time()', 'remaining = 10.0', 1),
            self.source.replace('setattr(storage_error, name, value)\n                    raise',
                                'setattr(storage_error, name, value)\n                    raise preflight', 1),
        )
        for mutant in mutants:
            with self.subTest(mutant=mutant), TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.registry_fixture(root)
                self.assertEqual(registry_violations(root, registry), [])
                self.assertNotEqual(mutant, self.source)
                (root / 'kb_platform/remote.py').write_text(mutant, encoding='utf-8')
                for binding in registry['machines'][0]['source_bindings']:
                    binding['sha256'] = source_digest(root, binding)
                errors = registry_violations(root, registry)
                self.assertTrue(any(error.startswith('provider_admission:') for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)

    def test_canonical_error_module_binding_detects_constant_field_loss(self):
        actual = json.loads((Path(__file__).resolve().parents[2] / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(machine for machine in actual['machines'] if machine['id'] == 'provider_admission')
        self.assertTrue(any(binding['path'] == 'kb_platform/model_errors.py' and binding['symbol'] == '<module>'
                            for binding in machine['source_bindings']))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.registry_fixture(root)
            self.assertEqual(registry_violations(root, registry), [])
            path = root / 'kb_platform/model_errors.py'
            binding = {'path': 'kb_platform/model_errors.py', 'language': 'python', 'symbol': 'error_details'}
            original_function = source_digest(root, binding)
            text = path.read_text('utf-8')
            mutant = text.replace(", 'known_zero_send')", ')', 1)
            self.assertNotEqual(mutant, text)
            path.write_text(mutant, encoding='utf-8')
            self.assertEqual(source_digest(root, binding), original_function)
            self.assertTrue(any('source review drift at kb_platform/model_errors.py:<module>' in error
                                for error in registry_violations(root, registry)))
