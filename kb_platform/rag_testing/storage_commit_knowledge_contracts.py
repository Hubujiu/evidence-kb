"""Offline implementation and structural contracts for transaction outcome evidence."""
import ast
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import select

from kb_platform.database_contracts import CommitUnknown
from kb_platform.persistence_schema import budget_calls, budget_schema

from .storage_structured_wire_contracts import structured


def outcome_evidence_violations(source):
    """Require explicit exit acknowledgement before publishing a terminal receipt."""
    tree = ast.parse(source)
    handle = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == '_Handle')
    run = next(node for node in handle.body if isinstance(node, ast.FunctionDef) and node.name == '_run')
    errors, initial = [], []

    def visit(body, acknowledged=False):
        for node in body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                callback = node.value.func
                if (isinstance(callback, ast.Attribute) and callback.attr == '__exit__'
                        and isinstance(callback.value, ast.Name) and callback.value.id == 'manager'):
                    acknowledged = True
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == 'outcome':
                        initial.append(node.value)
                    if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                            and target.value.id == 'outcome' and isinstance(target.slice, ast.Constant)
                            and target.slice.value == 'status'):
                        known = any(isinstance(item, ast.Constant) and item.value in {'COMMITTED', 'ROLLED_BACK', 'RELEASED'}
                                    for item in ast.walk(node.value))
                        if known and not acknowledged:
                            errors.append('terminal outcome requires preceding normal manager exit')
            for field in ('body', 'orelse', 'finalbody'):
                children = getattr(node, field, None)
                if isinstance(children, list):
                    # A try/except does not acknowledge a failed exit. Its successful
                    # body may promote only after an exit in that same control path.
                    visit(children, acknowledged if not isinstance(node, ast.Try) else False)
            for handler in getattr(node, 'handlers', []):
                visit(handler.body, False)

    visit(run.body)
    if len(initial) != 1 or not isinstance(initial[0], ast.Dict):
        errors.append('one outcome initializer required')
    else:
        values = {key.value: value for key, value in zip(initial[0].keys, initial[0].values)
                  if isinstance(key, ast.Constant)}
        if not isinstance(values.get('status'), ast.Constant) or values['status'].value != 'UNKNOWN':
            errors.append('outcome must start UNKNOWN')
        if ast.unparse(values.get('operation_id', ast.Constant(None))) != 'self.operation_id':
            errors.append('outcome must retain operation identity')
    return errors


class StorageCommitKnowledgeContracts(unittest.TestCase):
    @staticmethod
    def row(identity):
        return {'id': identity, 'status': 'UNKNOWN', 'reserved': '{}', 'usage': None,
                'category': 'synthetic', 'diagnostic': None}

    def test_post_commit_cleanup_failure_keeps_actual_sqlite_write_but_no_rollback_receipt(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            original, attempts = endpoint._store.transaction, []

            @contextmanager
            def commit_then_cleanup_error(**options):
                with original(**options) as transaction:
                    yield transaction
                attempts.append(options['operation_id'])
                raise RuntimeError('synthetic post-commit cleanup error')

            with patch.object(endpoint._store, 'transaction', side_effect=commit_then_cleanup_error):
                with self.assertRaises(CommitUnknown) as caught:
                    with client.transaction(operation_id='committed-without-ack') as transaction:
                        transaction.insert(budget_calls, self.row('persisted'))
            self.assertEqual(caught.exception.operation_id, 'committed-without-ack')
            self.assertEqual(attempts, ['committed-without-ack'])
            self.assertEqual(endpoint._outcomes[transaction._token]['status'], 'UNKNOWN')
            for commit in (True, False, True):
                with self.assertRaises(CommitUnknown) as repeated:
                    endpoint.execute({'version': 1, 'op': 'finish', 'handle': transaction._token, 'commit': commit})
                self.assertEqual(repeated.exception.operation_id, 'committed-without-ack')
            with client.transaction(write=False) as read:
                self.assertEqual([row['id'] for row in read.execute(select(budget_calls)).rows], ['persisted'])
            self.assertEqual(attempts, ['committed-without-ack'])

    def test_rollback_cleanup_failure_is_unknown_even_when_sqlite_actually_rolled_back(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            original = endpoint._store.transaction

            @contextmanager
            def rollback_then_cleanup_error(**options):
                try:
                    with original(**options) as transaction:
                        yield transaction
                finally:
                    raise RuntimeError('synthetic rollback cleanup error')

            with patch.object(endpoint._store, 'transaction', side_effect=rollback_then_cleanup_error):
                with self.assertRaisesRegex(ValueError, 'body failure'):
                    with client.transaction(operation_id='rollback-without-ack') as transaction:
                        transaction.insert(budget_calls, self.row('not-persisted'))
                        raise ValueError('body failure')
            self.assertEqual(endpoint._outcomes[transaction._token]['status'], 'UNKNOWN')
            with self.assertRaises(CommitUnknown) as repeated:
                endpoint.execute({'version': 1, 'op': 'finish', 'handle': transaction._token, 'commit': True})
            self.assertEqual(repeated.exception.operation_id, 'rollback-without-ack')
            with client.transaction(write=False) as read:
                self.assertEqual(read.execute(select(budget_calls)).rows, ())

    def test_provider_unknown_and_observed_normal_outcomes_stay_distinct(self):
        schema = budget_schema('budget')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            with client.transaction(operation_id='normal-commit') as committed:
                committed.insert(budget_calls, self.row('normal'))
            self.assertEqual(endpoint._outcomes[committed._token]['status'], 'COMMITTED')
            with self.assertRaises(ValueError):
                with client.transaction(operation_id='normal-rollback') as rolled_back:
                    raise ValueError('normal rollback')
            self.assertEqual(endpoint._outcomes[rolled_back._token]['status'], 'ROLLED_BACK')
            original = endpoint._store.transaction

            @contextmanager
            def provider_unknown(**options):
                with original(**options) as transaction:
                    yield transaction
                raise CommitUnknown(options['operation_id'])

            with patch.object(endpoint._store, 'transaction', side_effect=provider_unknown):
                with self.assertRaises(CommitUnknown):
                    with client.transaction(operation_id='provider-unknown') as unknown:
                        pass
            self.assertEqual(endpoint._outcomes[unknown._token],
                             {'status': 'UNKNOWN', 'operation_id': 'provider-unknown'})
            with self.assertRaises(CommitUnknown):
                endpoint.execute({'version': 1, 'op': 'finish', 'handle': unknown._token, 'commit': True})

    def test_source_terminal_receipts_require_normal_exit_acknowledgement(self):
        source = (Path(__file__).parents[1] / 'storage_structured_wire.py').read_text('utf-8')
        self.assertEqual(outcome_evidence_violations(source), [])
        mutants = [source.replace("outcome = {'status': 'UNKNOWN'", "outcome = {'status': 'ROLLED_BACK'", 1),
                   source.replace("manager.__exit__(None, None, None)",
                                  "outcome['status'] = 'COMMITTED'\n                        manager.__exit__(None, None, None)", 1),
                   source.replace('except BaseException as cleanup_error:',
                                  "except BaseException as cleanup_error:\n                    outcome['status'] = 'ROLLED_BACK'", 1)]
        for mutant in mutants:
            self.assertNotEqual(mutant, source)
            self.assertTrue(outcome_evidence_violations(mutant))
