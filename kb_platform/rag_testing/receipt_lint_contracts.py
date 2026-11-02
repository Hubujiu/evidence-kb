from pathlib import Path
import unittest

from scripts.receipt_transition_checks import receipt_transition_violations, sqlite_guard_violations


ROOT = Path(__file__).resolve().parents[2]


class ReceiptLintContracts(unittest.TestCase):
    def setUp(self):
        self.receipts = (ROOT / 'kb_platform/provider_runtime.py').read_text('utf-8')
        self.guard = (ROOT / 'kb_platform/database_providers/sqlite_guard.py').read_text('utf-8')

    def test_actual_receipt_transitions_and_shared_sqlite_guard_satisfy_lint(self):
        self.assertEqual(receipt_transition_violations(self.receipts), [])
        self.assertEqual(sqlite_guard_violations(self.guard), [])

    def test_lint_rejects_removed_guards_changed_identity_and_unprotected_publication(self):
        guard = "with sqlite_guard(self._pending(logical_id).with_suffix('.guard.sqlite3'), nowait=False):"
        mutants = (
            self.receipts.replace(guard, 'with nullcontext():', 1),
            self.receipts.replace(".with_suffix('.guard.sqlite3'), nowait=False", ".with_suffix('.other.sqlite3'), nowait=False"),
            self.receipts.replace("or active is None or active['physical_request_id'] != physical_id", 'or active is None'),
            self.receipts.replace('            self._write(physical_id, row)\n            if status',
                                  '        self._write(physical_id, row)\n        if status'),
            self.receipts.replace(guard, 'self._pending(logical_id).unlink(missing_ok=True)\n        ' + guard, 1),
        )
        for index, source in enumerate(mutants):
            with self.subTest(mutant=index):
                try:
                    errors = receipt_transition_violations(source)
                except SyntaxError:
                    self.fail('lint mutant must remain parseable')
                self.assertTrue(errors)

    def test_lint_rejects_nonexclusive_lock_early_yield_and_guard_removal(self):
        mutants = (
            self.guard.replace("'BEGIN IMMEDIATE'", "'BEGIN'"),
            self.guard.replace('        yield\n', '').replace('    try:\n', '    try:\n        yield\n', 1),
            self.guard.replace('        connection.close()', '        connection.commit()'),
            self.guard.replace('        connection.close()', '        connection.close()\n        path.unlink()'),
        )
        for index, source in enumerate(mutants):
            with self.subTest(mutant=index):
                self.assertTrue(sqlite_guard_violations(source))

    def test_lint_accepts_nonsemantic_declarations_and_spacing_changes(self):
        self.assertEqual(receipt_transition_violations(self.receipts.replace(
            '    def begin(self, logical_id, request_hash, metadata):',
            '    def begin(self, logical_id: str, request_hash, metadata):')), [])
        self.assertEqual(sqlite_guard_violations(self.guard.replace(
            "connection.execute('BEGIN IMMEDIATE')", 'connection.execute( "BEGIN IMMEDIATE" )')), [])
