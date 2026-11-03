import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kb_platform.rag_testing.formal_state_machines import registry_violations, source_digest
from kb_platform.rag_testing.native_sessions_lint import native_sessions_violations

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'rust/storage-service/src/service.rs'
MODEL = 'formal/native_sessions/NativeSessions.tla'


class NativeSessionsFormalContracts(unittest.TestCase):
    def test_native_table_lifecycle_seams(self):
        self.assertEqual(native_sessions_violations((ROOT / SOURCE).read_text('utf-8'), (ROOT / MODEL).read_text('utf-8')), [])

    def test_mandatory_guard_rejects_refreshed_source_hash_mutants(self):
        original = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(m for m in original['machines'] if m['id'] == 'native_storage_sessions')
        registry = {'schema_version': 1, 'discovery': {'roots': ['rust'], 'exclusions': []}, 'machines': [machine]}
        paths = {SOURCE} | {r[k] for r in machine['models'] + machine['negative_controls'] for k in ('module', 'config')}
        mutations = [
            ('sessions.values.len() >= 64', 'sessions.values.len() > 64'),
            ('session.touched.elapsed() < Duration::from_secs(300)', 'true'),
            ('sessions.next.checked_add(1).ok_or_else(state_error)?', '1'),
            ('sessions.values.remove(&handle).ok_or_else(state_error)?', 'sessions.values.get(&handle).ok_or_else(state_error)?'),
            ('session.phase != Phase::Executing', 'false'),
            ('session.phase != Phase::Input', 'false'),
            ('session.codec = None;', ''),
            ('session.phase == Phase::Values', 'false'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, root / path)
            self.assertEqual(registry_violations(root, registry), [])
            source = (ROOT / SOURCE).read_text('utf-8')
            for before, after in mutations:
                with self.subTest(before=before):
                    changed = source.replace(before, after, 1)
                    self.assertNotEqual(changed, source)
                    (root / SOURCE).write_text(changed, 'utf-8')
                    revised = copy.deepcopy(registry)
                    for binding in revised['machines'][0]['source_bindings']:
                        binding['sha256'] = source_digest(root, binding)
                    errors = registry_violations(root, revised)
                    self.assertTrue(any('native_storage_sessions:' in error for error in errors), errors)
                    self.assertFalse(any('source review drift' in error for error in errors))

    def test_witnesses_cannot_claim_close_as_expiry_or_failure_entry_as_cleanup(self):
        source = (ROOT / SOURCE).read_text('utf-8')
        model = (ROOT / MODEL).read_text('utf-8')
        for before, after in [
            ('WitnessExpiry == ~(expired # {}', 'WitnessExpiry == ~(next > 0'),
            (r'WitnessFailedCleanup == ~(failed \intersect removed # {}', 'WitnessFailedCleanup == ~(failed # {}'),
        ]:
            changed = model.replace(before, after, 1)
            self.assertNotEqual(changed, model)
            self.assertTrue(native_sessions_violations(source, changed))
