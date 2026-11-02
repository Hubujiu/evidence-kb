import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.physical_settlement_lint import CAPTURE_CONSUMERS, OWNERS, settlement_violations


ROOT = Path(__file__).resolve().parents[2]


class PhysicalSettlementLintContracts(unittest.TestCase):
    def fixture(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for path in {owner[0] for owner in (*OWNERS, *CAPTURE_CONSUMERS)}:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((ROOT / path).read_text('utf-8'), 'utf-8')
        return root

    def mutate(self, root, owner, mutate):
        path, class_name, method_name, _ = owner
        target = root / path
        tree = ast.parse(target.read_text('utf-8'))
        group = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
        method = next(node for node in group.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
        helper = next(node for node in method.body if isinstance(node, ast.FunctionDef) and node.name == 'settle')
        attempt = next(node for node in method.body if isinstance(node, ast.Try) and any(
            isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == 'settle'
            for statement in node.body for item in ast.walk(statement)))
        mutate(method, helper, attempt)
        target.write_text(ast.unparse(ast.fix_missing_locations(tree)) + '\n', 'utf-8')

    def test_current_sources_and_unrelated_edits_are_accepted(self):
        self.assertEqual(settlement_violations(ROOT), [])
        root = self.fixture()
        def unrelated(method, helper, attempt):
            method.body.insert(0, ast.parse("unrelated_diagnostic = 'synthetic lint fixture'").body[0])
            method.body.insert(method.body.index(attempt), ast.parse('len(record)').body[0])

        for owner in OWNERS:
            self.mutate(root, owner, unrelated)
        self.assertEqual(settlement_violations(root), [])

    def test_duplicate_settlement_and_swallowed_commit_identity_are_rejected(self):
        def duplicate(method, helper, attempt):
            attempt.handlers[0].body.insert(2, ast.parse("self.ledger.finish('duplicate', status='UNKNOWN')").body[0])

        def replace_error(method, helper, attempt):
            helper.body[1].handlers[0].body[-1] = ast.parse("raise RuntimeError('masked')").body[0]

        def swallow(method, helper, attempt):
            helper.body[1].handlers[0].body[-1] = ast.Return(value=ast.Constant(value=None))

        def remove_guard(method, helper, attempt):
            del attempt.handlers[0].body[1]

        def erase_marker(method, helper, attempt):
            helper.body[1].handlers[0].body.insert(0, ast.parse("record.pop('ledger_settlement')").body[0])

        mutants = ((duplicate, 'physical finish must occur once'),
                   (replace_error, 'preserve original exception'),
                   (swallow, 'preserve original exception'),
                   (remove_guard, 'immediately rethrow'),
                   (erase_marker, 'marker must not be erased'))
        for owner in OWNERS:
            for mutate, diagnostic in mutants:
                with self.subTest(owner=owner[1], mutation=mutate.__name__):
                    root = self.fixture()
                    self.mutate(root, owner, mutate)
                    self.assertTrue(any(diagnostic in error for error in settlement_violations(root)))

    def test_marker_order_false_confirmation_and_loop_repetition_are_rejected(self):
        def mark_late(method, helper, attempt):
            helper.body[0], helper.body[1] = helper.body[1], helper.body[0]

        def finally_confirm(method, helper, attempt):
            helper.body[1].finalbody = [helper.body.pop()]

        def repeat(method, helper, attempt):
            for index, node in enumerate(attempt.body):
                if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name) and node.value.func.id == 'settle'):
                    attempt.body[index] = ast.For(target=ast.Name(id='again', ctx=ast.Store()),
                        iter=ast.Tuple(elts=[ast.Constant(value=0), ast.Constant(value=1)], ctx=ast.Load()),
                        body=[node], orelse=[])
                    return
            self.fail('synthetic settlement insertion point missing')

        for owner in OWNERS:
            for mutate, diagnostic in ((mark_late, 'mark before finish'),
                                       (finally_confirm, 'confirm only after success'),
                                       (repeat, 'must not be repeated by a loop')):
                with self.subTest(owner=owner[1], mutation=mutate.__name__):
                    root = self.fixture()
                    self.mutate(root, owner, mutate)
                    self.assertTrue(any(diagnostic in error for error in settlement_violations(root)))

    def test_terminal_only_predicate_and_removed_consumer_gates_are_rejected(self):
        root = self.fixture()
        path = root / 'kb_platform/rag_testing/provider_capture.py'
        tree = ast.parse(path.read_text('utf-8'))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'capture_unresolved')
        helper.body[0].value = ast.parse("record.get('status') not in ('COMPLETE', 'ERROR')", mode='eval').body
        path.write_text(ast.unparse(tree), 'utf-8')
        self.assertTrue(any('terminal provider status' in error for error in settlement_violations(root)))

        class RemovePredicate(ast.NodeTransformer):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == 'capture_unresolved':
                    return ast.Constant(value=False)
                return self.generic_visit(node)

        for path, symbol, _, _ in CAPTURE_CONSUMERS:
            with self.subTest(consumer=symbol):
                root = self.fixture()
                target = root / path
                tree = ast.parse(target.read_text('utf-8'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == symbol.rsplit('.', 1)[-1]:
                        RemovePredicate().visit(node)
                target.write_text(ast.unparse(ast.fix_missing_locations(tree)), 'utf-8')
                self.assertTrue(any(symbol + ': workflow must gate' in error for error in settlement_violations(root)))

    def test_missing_or_unparseable_owner_fails_closed(self):
        for content in (None, 'class invalid syntax'):
            with self.subTest(content=content):
                root = self.fixture()
                target = root / OWNERS[0][0]
                if content is None:
                    target.unlink()
                else:
                    target.write_text(content, 'utf-8')
                self.assertTrue(any('owner unreadable' in error for error in settlement_violations(root)))
