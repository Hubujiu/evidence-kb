from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.receipt_database_checks import receipt_database_violations, receipt_database_transition_violations, receipt_send_order_violations
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class ReceiptDatabaseLintContracts(unittest.TestCase):
    def test_actual_transaction_and_transition_guards_accept_annotations_and_spacing(self):
        database = (ROOT / 'kb_platform/provider_receipt_database.py').read_text('utf-8')
        runtime = (ROOT / 'kb_platform/provider_runtime.py').read_text('utf-8')
        self.assertEqual(receipt_database_violations(database), [])
        self.assertEqual(receipt_database_transition_violations(runtime), [])
        self.assertEqual(receipt_send_order_violations((ROOT / 'kb_platform/remote.py').read_text('utf-8')), [])
        self.assertEqual(receipt_database_violations(database.replace('def begin(self, logical_id, row, validate):',
            'def begin(self, logical_id: str, row, validate):')), [])
        self.assertEqual(receipt_database_transition_violations(runtime.replace(
            "row['status'] != 'STARTED'", 'row["status"] != "STARTED"')), [])

    def test_mandatory_registry_lint_rejects_database_mutants_with_refreshed_source_hashes(self):
        database_path = 'kb_platform/provider_receipt_database.py'
        runtime_path = 'kb_platform/provider_runtime.py'
        guard_path = 'kb_platform/database_providers/sqlite_guard.py'
        remote_path = 'kb_platform/remote.py'
        sources = {path: (ROOT / path).read_text('utf-8') for path in (database_path, runtime_path, guard_path, remote_path)}
        database, runtime = sources[database_path], sources[runtime_path]
        mutations = (
            (database_path, database.replace('self.database.transaction(write=True)', 'self.database.transaction(write=False)')),
            (database_path, database.replace('.with_for_update()', '')),
            (database_path, database.replace("'row_json': '{}'", "'row_json': 'missing'")),
            (database_path, database.replace('            mutate(value, active)\n', '')),
            (database_path, database.replace('        return row[\'physical_request_id\']', '            return row[\'physical_request_id\']')),
            (database_path, database.replace('db.execute(receipt_logical.update()', 'other.execute(receipt_logical.update()')),
            (runtime_path, runtime.replace("or row['logical_request_id'] != logical_id", '')),
            (runtime_path, runtime.replace("or active['physical_request_id'] != physical_id", '')),
            (runtime_path, runtime.replace("previous['status'] in {'STARTED', 'UNKNOWN'}", "previous['status'] in {'STARTED'}")),
            (runtime_path, runtime.replace('or not evidence_reference.strip()', '')),
            (remote_path, sources[remote_path].replace("logical_id = metadata['request_id']",
                "self._request_response(path, payload, headers, self.timeout)\n        logical_id = metadata['request_id']")),
        )
        for target, changed in mutations:
            with self.subTest(target=target), TemporaryDirectory() as directory:
                self.assertNotEqual(sources[target], changed)
                root = Path(directory)
                (root / 'formal').mkdir()
                bindings = []
                for path, source in sources.items():
                    destination = root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(source, encoding='utf-8')
                    binding = {'path': path, 'language': 'python', 'symbol': '<module>'}
                    binding['sha256'] = source_digest(root, binding)
                    bindings.append(binding)
                registry = {'schema_version': 1, 'discovery': {'roots': ['kb_platform'], 'exclusions': []},
                    'machines': [{'id': 'provider_receipts', 'status': 'unmodeled', 'owners': list(sources),
                        'states': ['STARTED', 'COMPLETE'], 'transitions': ['begin', 'finish', 'reconcile'],
                        'invariants': ['Atomic receipt pair and identity validation'],
                        'assumptions': ['Synthetic lint fixture'], 'exclusions': ['Not model coverage'],
                        'source_bindings': bindings, 'models': [], 'negative_controls': []}]}
                self.assertEqual(registry_violations(root, registry), [])
                (root / target).write_text(changed, encoding='utf-8')
                for binding in bindings:
                    binding['sha256'] = source_digest(root, binding)
                errors = registry_violations(root, registry)
                self.assertTrue(any('receipt' in error and 'source review drift' not in error for error in errors), errors)
                self.assertFalse(any('source review drift' in error for error in errors), errors)


if __name__ == '__main__':
    unittest.main()
