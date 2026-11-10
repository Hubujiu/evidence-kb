from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from scripts.physical_ledger_checks import ledger_core_violations, local_ranker_settlement_violations
from .budget import BudgetLedger
from . import formal_state_machine_contracts
from .formal_state_machines import registry_violations, source_digest


ROOT = Path(__file__).resolve().parents[2]


class PhysicalLedgerCoreContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.caps = {'input': 2, 'output': 2}

    def ledger(self, name='budget'):
        ledger = BudgetLedger(self.root / (name + '.sqlite3'), self.caps)
        self.addCleanup(ledger.close)
        return ledger

    def test_partial_usage_distinguishes_known_zero_from_unreported_dimension_after_reopen(self):
        ledger = self.ledger()
        ledger.reserve('partial', {'input': 1, 'output': 1})
        ledger.finish('partial', status='ERROR', usage={'input': 0})
        summary = self.ledger().summary()
        self.assertEqual(summary['effective_usage_or_reserved'], {'input': 0, 'output': 1})
        self.assertEqual(summary['unknown_usage_requests'], 1)
        self.assertEqual(summary['pending_or_unknown'], 0)
        self.assertFalse(summary['over_budget'])
        ledger.reserve('next', {'input': 2, 'output': 1}, require_settled=True)
        self.assertEqual(ledger.summary()['effective_usage_or_reserved'], self.caps)

    def test_actual_over_cap_is_not_clipped_and_blocks_even_zero_new_reservation(self):
        ledger = self.ledger()
        ledger.reserve('observed', {'input': 1, 'output': 1})
        ledger.finish('observed', status='COMPLETE', usage={'input': 3, 'output': 0})
        reopened = self.ledger()
        self.assertEqual(reopened.entry('observed')['usage'], {'input': 3, 'output': 0})
        self.assertTrue(reopened.summary()['over_budget'])
        for strict in (False, True):
            with self.subTest(require_settled=strict):
                with self.assertRaisesRegex(ValueError, 'BUDGET_INSUFFICIENT'):
                    reopened.reserve('later', {'input': 0, 'output': 0}, require_settled=strict)
        self.assertEqual(reopened.summary()['physical_requests'], 1)

    def test_require_settled_is_explicit_policy_not_unconditional_unknown_refund(self):
        for status in ('STARTED', 'UNKNOWN'):
            with self.subTest(status=status):
                ledger = self.ledger(status)
                ledger.reserve('prior', {'input': 1, 'output': 1})
                if status == 'UNKNOWN':
                    ledger.finish('prior', status='UNKNOWN', usage={'input': 0})
                with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PHYSICAL_REQUEST'):
                    ledger.reserve('next', {'input': 1, 'output': 1}, require_settled=True)
                ledger.reserve('next', {'input': 1, 'output': 1})
                expected = {'input': 1 if status == 'UNKNOWN' else 2, 'output': 2}
                self.assertEqual(ledger.summary()['effective_usage_or_reserved'], expected)
                self.assertEqual(ledger.summary()['pending_or_unknown'], 2)

    def test_two_finish_callers_have_one_cas_winner_and_terminal_row_never_changes(self):
        first, second = self.ledger(), self.ledger()
        first.reserve('same', {'input': 1, 'output': 1})
        barrier = Barrier(2, timeout=5)

        def finish(ledger, status, usage):
            barrier.wait()
            try:
                ledger.finish('same', status=status, usage=usage)
                return status, usage
            except ValueError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(finish, first, 'COMPLETE', {'input': 3, 'output': 0}),
                       workers.submit(finish, second, 'UNKNOWN', {'input': 0})]
            outcomes = [future.result(timeout=10) for future in futures]
        accepted = [value for value in outcomes if isinstance(value, tuple)]
        rejected = [value for value in outcomes if isinstance(value, ValueError)]
        self.assertEqual(len(accepted), 1, outcomes)
        self.assertEqual([str(error) for error in rejected], ['missing or terminal physical request'])
        original = self.ledger().entry('same')
        self.assertEqual((original['status'], original['usage']), accepted[0])
        for status in ('COMPLETE', 'ERROR', 'UNKNOWN'):
            with self.subTest(status=status):
                with self.assertRaisesRegex(ValueError, 'missing or terminal physical request'):
                    second.finish('same', status=status, usage={'input': 0, 'output': 0})
                self.assertEqual(first.entry('same'), original)
        with self.assertRaisesRegex(ValueError, 'missing or terminal physical request'):
            second.finish('missing', status='COMPLETE')
        self.assertEqual(first.summary()['physical_requests'], 1)


class PhysicalLedgerSourceContracts(unittest.TestCase):
    def test_current_sources_formatting_and_missing_owner_detection(self):
        budget = (ROOT / 'kb_platform/rag_testing/budget.py').read_text('utf-8')
        ranker = (ROOT / 'kb_platform/rag_testing/local_rerank.py').read_text('utf-8')
        self.assertEqual(ledger_core_violations(budget + '\n# unrelated formatting\n'), [])
        self.assertEqual(local_ranker_settlement_violations(ranker + '\n# unrelated formatting\n'), [])
        for source in ('invalid syntax:', ''):
            self.assertTrue(ledger_core_violations(source))
            self.assertTrue(local_ranker_settlement_violations(source))

    def test_semantic_regressions_fail_mandatory_registry_even_after_source_hash_refresh(self):
        mutations = {
            'kb_platform/rag_testing/budget.py': (
                ('actual.get(resource, reserve[resource])', 'actual.get(resource, 0)'),
                ("budget_calls.c.status == 'STARTED'", "budget_calls.c.status != 'absent'"),
                ('cursor.rowcount != 1', 'cursor.rowcount < 0'),
                ('used[k] + resources[k] > cap', 'used[k] + resources[k] > cap + 100'),
                ('if require_settled and db.execute(', 'if False and db.execute('),
                ('*, require_settled=False', '*, require_settled=True'),
                ('any(totals[k] > cap for k, cap in self.caps.items())', 'False'),
            ),
            'kb_platform/rag_testing/local_rerank.py': (
                ("            else:\n                self.ledger.finish(physical_id, status='COMPLETE', usage=resources)",
                 "            finally:\n                self.ledger.finish(physical_id, status='COMPLETE', usage=resources)"),
                ("diagnostic=str(exc))\n                raise", "diagnostic=str(exc))\n                raise RuntimeError('masked')"),
            ),
        }
        for path, variants in mutations.items():
            original = (ROOT / path).read_text('utf-8')
            for before, after in variants:
                with self.subTest(path=path, mutation=before), TemporaryDirectory() as directory:
                    root = Path(directory)
                    registry = formal_state_machine_contracts.FormalRegistryContracts().fixture(root)
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(original, 'utf-8')
                    binding = {'path': path, 'language': 'python', 'symbol': '<module>'}
                    binding['sha256'] = source_digest(root, binding)
                    registry['machines'][0]['source_bindings'].append(binding)
                    self.assertEqual(registry_violations(root, registry), [])
                    self.assertEqual(original.count(before), 1)
                    target.write_text(original.replace(before, after), 'utf-8')
                    binding['sha256'] = source_digest(root, binding)
                    errors = registry_violations(root, registry)
                    self.assertTrue(any('physical ledger core:' in value or 'local ranker settlement:' in value
                                        for value in errors), errors)
                    self.assertFalse(any('source review drift' in value for value in errors), errors)
