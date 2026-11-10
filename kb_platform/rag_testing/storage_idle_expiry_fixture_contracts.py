import ast
from pathlib import Path
import unittest


def idle_expiry_fixture_violations(source):
    tree = ast.parse(source)
    test = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                and node.name == 'test_expired_transaction_rolls_back_and_repeated_finish_observes_same_outcome')
    functions = {node.name: node for node in ast.walk(test) if isinstance(node, ast.FunctionDef)}
    errors = []
    confirm = functions.get('confirm_insert')
    expected = ['result = original_insert(tx, *args, **kwargs)', 'self.assertEqual(result.rowcount, 1)', 'inserted.set()']
    if confirm is None or [ast.unparse(node) for node in confirm.body[:3]] != expected:
        errors.append('confirm actual insert rowcount before enabling idle expiry')
    get = functions.get('get')
    timed = get.body[0] if get is not None and get.body else None
    ready = timed.body[0] if isinstance(timed, ast.If) and timed.body else None
    if (not isinstance(timed, ast.If) or ast.unparse(timed.test) != 'block and timeout is not None'
            or not isinstance(ready, ast.If) or ast.unparse(ready.test) != 'inserted.is_set()'
            or [ast.unparse(node) for node in ready.body] != ['expired.append(timeout)', 'raise Empty']
            or len(timed.body) != 2 or ast.unparse(timed.body[1]) != 'return super().get()'
            or sum(isinstance(node, ast.Raise) for node in ast.walk(get)) != 1):
        errors.append('queue.Empty must be gated by confirmed write in a timed idle get')
    originals = [node for node in ast.walk(test) if isinstance(node, ast.Assign)
                 and ast.unparse(node) == 'original_insert = StructuredTransaction.insert']
    if len(originals) != 1 or confirm is None or originals[0].lineno >= confirm.lineno:
        errors.append('wrap the actual StructuredTransaction.insert implementation')
    calls = [node for node in ast.walk(test) if isinstance(node, ast.Call)]
    normalized = {ast.unparse(node) for node in calls}
    required = {
        "patch('kb_platform.database_providers.base.monotonic', return_value=0.0)",
        "patch('kb_platform.storage_structured_wire.monotonic', return_value=0.0)",
        "patch('kb_platform.storage_structured_wire.Queue', IdleExpiryQueue)",
        "patch.object(StructuredTransaction, 'insert', confirm_insert)",
        'QueryLimits(max_seconds=0.2)',
        "self.assertRaisesRegex(DatabaseError, 'STORAGE_TRANSACTION_ROLLED_BACK')",
        'self.assertEqual(expired, [0.2])',
    }
    if not required <= normalized:
        errors.append('preserve isolated clocks, phase queue, .2 budget and exact rollback assertion')
    captures = [node.lineno for node in ast.walk(test) if isinstance(node, ast.Assign)
                and ast.unparse(node) == 'handle = endpoint._handles[transaction._token]']
    inserts = [node.lineno for node in calls if ast.unparse(node.func) == 'transaction.insert']
    if len(captures) != 1 or len(inserts) != 1 or captures[0] >= inserts[0]:
        errors.append('capture worker before confirmed write can trigger idle expiry')
    return errors


class StorageIdleExpiryFixtureContracts(unittest.TestCase):
    def source(self):
        return Path(__file__).with_name('storage_structured_wire_contracts.py').read_text('utf-8')

    def test_idle_expiry_fixture_preserves_success_before_timeout_boundary(self):
        self.assertEqual(idle_expiry_fixture_violations(self.source()), [])

    def test_seeded_early_expiry_wall_clock_and_budget_mutants_are_rejected(self):
        source = self.source()
        mutants = [
            source.replace('if inserted.is_set():', 'if True:', 1),
            source.replace('return super().get()', 'return super().get(timeout=timeout)', 1),
            source.replace('self.assertEqual(result.rowcount, 1)\n                inserted.set()',
                           'inserted.set()\n                self.assertEqual(result.rowcount, 1)', 1),
            source.replace("patch('kb_platform.storage_structured_wire.Queue', IdleExpiryQueue)",
                           "patch('kb_platform.storage_structured_wire.Queue', Queue)", 1),
            source.replace("patch('kb_platform.database_providers.base.monotonic', return_value=0.0)",
                           "patch('kb_platform.database_providers.base.monotonic')", 1),
            source.replace('QueryLimits(max_seconds=.2)', 'QueryLimits(max_seconds=2)', 1),
        ]
        for mutant in mutants:
            self.assertNotEqual(mutant, source)
            self.assertTrue(idle_expiry_fixture_violations(mutant))
