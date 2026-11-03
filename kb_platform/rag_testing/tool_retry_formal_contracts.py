import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kb_platform.rag_testing.formal_state_machines import registry_violations, source_digest
from kb_platform.rag_testing.tool_retry_lint import OWNER, CALLER, FIXTURE, tool_retry_source_violations, tool_fixture_source_violations


ROOT = Path(__file__).resolve().parents[2]


class ToolRetryFormalContracts(unittest.TestCase):
    def test_actual_tool_retry_owner_matches_reviewed_structure(self):
        self.assertEqual(tool_retry_source_violations((ROOT / OWNER).read_text(), (ROOT / CALLER).read_text()), [])
        self.assertEqual(tool_fixture_source_violations((ROOT / FIXTURE).read_text()), [])

    def test_seeded_policy_budget_receipt_and_namespace_mutants_are_rejected(self):
        source, caller = (ROOT / OWNER).read_text(), (ROOT / CALLER).read_text()
        mutants = [
            ('not read_only or not classify_failure(retry_state.outcome.exception()).retryable', 'False'),
            ('not read_only or ', ''),
            ('not classify_failure(retry_state.outcome.exception()).retryable', 'False'),
            ('stop_after_attempt(self.max_attempts)(retry_state)', 'False'),
            ('retry_state.idle_for + retry_state.upcoming_sleep > self.max_wait_seconds', 'False'),
            ('time.monotonic() - started + retry_state.upcoming_sleep >= deadline_seconds', 'False'),
            ("hint = getattr(exc, 'retry_after_seconds', None)", 'hint = None'),
            ('math.isfinite(hint)', 'True'),
            ('delay = max(delay, hint)', 'delay = min(delay, hint)'),
            ("raise RuntimePolicyError('TOOL_REQUEST_IN_PROGRESS')", 'pass'),
            ("raise RuntimePolicyError('TOOL_REQUEST_ID_REUSED')", 'pass'),
            ("deepcopy(existing['outcome'])", "existing['outcome']"),
            ("self._receipts[request_id]['outcome'] = deepcopy(outcome)", 'pass'),
            ("ToolOutcome('UNKNOWN', None, attempt, 'outcome_unknown'", "ToolOutcome('ERROR', None, attempt, 'outcome_unknown'"),
        ]
        for before, after in mutants:
            with self.subTest(mutation=before):
                self.assertIn(before, source)
                self.assertTrue(tool_retry_source_violations(source.replace(before, after, 1), caller))
        self.assertTrue(tool_retry_source_violations(source, caller.replace('self.session_id + ":" + self.request_id', 'self.session_id')))

    def test_mandatory_semantic_guard_rejects_legacy_faults_and_leaked_fixture_after_hash_refresh(self):
        machine = next(row for row in json.loads((ROOT / 'formal/registry-v1.json').read_text())['machines']
                       if row['id'] == 'tool_executor')
        registry = {'schema_version': 1, 'discovery': {'roots': [], 'exclusions': []}, 'machines': [machine]}
        paths = {row['path'] for row in machine['source_bindings']}
        paths.update(row[key] for row in machine['models'] + machine['negative_controls'] for key in ('module', 'config'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in paths:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, root / path)
            self.assertEqual(registry_violations(root, registry), [])
            originals = {path: (root / path).read_text() for path in paths}
            for path, before, after, expected in [
                (OWNER, 'not read_only or not classify_failure(retry_state.outcome.exception()).retryable', 'False',
                 'owned stop must independently'),
                (OWNER, "hint = getattr(exc, 'retry_after_seconds', None)", 'hint = None',
                 'wait must preserve finite nonnegative public'),
                (FIXTURE, 'closing(sqlite3.connect(path)) as connection, connection',
                 'sqlite3.connect(path) as connection', 'SQLite fixture connections must close'),
            ]:
                with self.subTest(mutation=before):
                    for name, original in originals.items():
                        (root / name).write_text(original)
                    self.assertIn(before, originals[path])
                    (root / path).write_text(originals[path].replace(before, after, 1))
                    for binding in machine['source_bindings']:
                        binding['sha256'] = source_digest(root, binding)
                    errors = registry_violations(root, registry)
                    self.assertTrue(any(expected in error for error in errors))
                    self.assertFalse(any('source review drift' in error for error in errors))
