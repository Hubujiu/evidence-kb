from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kb_platform.database_contracts import CommitUnknown, SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.provider_receipt_database import receipt_logical, receipt_physical
from kb_platform.provider_runtime import RequestReceipts, RuntimePolicyError
from kb_platform.remote import _Transport
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_sdk import ExistingDataset

from .budget import BudgetLedger


class ReceiptDatabaseTransitionContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.service = SqlStorageProvider(SQLiteProvider(self.root / 'databases'))
        self.addCleanup(self.service.close)

    def open(self, space='one'):
        key = SpaceKey('synthetic-receipts', 'receipt-owner', space)
        identity = self.service.initialize('structured', key)
        export = next(item for item in self.service.exports() if item.name == 'structured')
        return RequestReceipts(database=export.open_space(key, ExistingDataset(identity.dataset_id)))

    def rows(self, receipts):
        with receipts._database.database.transaction(write=False) as transaction:
            return {table.name: {row[key]: json.loads(row['row_json'])
                    for row in transaction.execute(select(table)).rows}
                    for table, key in ((receipt_logical, 'logical_hash'), (receipt_physical, 'physical_id'))}

    def path(self, receipts):
        return receipts._database.database._space.physical_name

    def transport(self, receipts):
        return SimpleNamespace(_receipts=receipts, _stats_lock=Lock(), _issued_requests=0,
                               max_requests=2, timeout=1,
                               _request_response=Mock(return_value=httpx.Response(200)))

    @contextmanager
    def lost_commit_ack(self, receipts, *, after=1):
        database = receipts._database.database
        transaction = database.transaction
        writes = 0

        @contextmanager
        def lose(*args, **kwargs):
            nonlocal writes
            with transaction(*args, **kwargs) as current:
                yield current
            if kwargs.get('write', True):
                writes += 1
                if writes == after:
                    raise CommitUnknown('synthetic-receipt-commit')

        with patch.object(database, 'transaction', side_effect=lose):
            yield

    def test_two_actual_sqlite_clients_admit_only_one_physical_request(self):
        first, second = self.open(), self.open()
        barrier = Barrier(2, timeout=10)

        def begin(receipts):
            barrier.wait()
            try:
                return 'accepted', receipts.begin('logical', 'payload', {})
            except RuntimePolicyError as error:
                return 'rejected', error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(begin, (first, second)))
        self.assertEqual(sum(kind == 'accepted' for kind, _ in results), 1)
        self.assertEqual([value for kind, value in results if kind == 'rejected'], ['PROVIDER_REQUEST_UNRESOLVED'])
        snapshot = self.rows(first)
        self.assertEqual(len(snapshot['receipt_logical']), 1)
        self.assertEqual(len(snapshot['receipt_physical']), 1)
        self.assertEqual(list(snapshot['receipt_logical'].values()), list(snapshot['receipt_physical'].values()))

    def test_finish_serializes_competing_finish_and_reconcile_and_hides_uncommitted_rows(self):
        for operation in ('finish', 'reconcile'):
            with self.subTest(operation=operation):
                first, second = self.open(operation), self.open(operation)
                physical = first.begin('logical', 'payload', {})
                original_save = first._database._save
                entered, release, attempted = Event(), Event(), Event()

                def pause(db, logical_hash, row, **options):
                    original_save(db, logical_hash, row, **options)
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError('SYNTHETIC_RECEIPT_BARRIER_TIMEOUT')

                def compete():
                    attempted.set()
                    if operation == 'finish':
                        return second.finish('logical', physical, 'UNKNOWN')
                    return second.reconcile('logical', physical, outcome='ERROR', evidence_reference='synthetic')

                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(first._database, '_save', side_effect=pause):
                    winner = pool.submit(first.finish, 'logical', physical, 'COMPLETE')
                    try:
                        self.assertTrue(entered.wait(10))
                        self.assertEqual(second._database.read(physical)['status'], 'STARTED')
                        with closing(sqlite3.connect(self.path(first), timeout=0)) as connection:
                            with self.assertRaises(sqlite3.OperationalError):
                                connection.execute('BEGIN IMMEDIATE')
                        loser = pool.submit(compete)
                        self.assertTrue(attempted.wait(10))
                    finally:
                        release.set()
                    winner.result(timeout=15)
                    with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_PHYSICAL_REQUEST_TRANSITION'
                                                if operation == 'finish' else 'INVALID_REQUEST_RECONCILIATION'):
                        loser.result(timeout=15)
                snapshot = self.rows(first)
                self.assertEqual(list(snapshot['receipt_logical'].values()), list(snapshot['receipt_physical'].values()))
                self.assertEqual(first._database.read(physical)['status'], 'COMPLETE')

    def test_trigger_failure_rolls_back_physical_and_logical_writes_together(self):
        for operation in ('begin', 'finish', 'reconcile'):
            with self.subTest(operation=operation):
                receipts = self.open(operation)
                physical = None if operation == 'begin' else receipts.begin('logical', 'payload', {})
                if operation == 'reconcile':
                    receipts.finish('logical', physical, 'UNKNOWN')
                before = self.rows(receipts)
                with closing(sqlite3.connect(self.path(receipts))) as connection, connection:
                    connection.execute("CREATE TRIGGER fail_logical BEFORE UPDATE ON receipt_logical "
                                       "BEGIN SELECT RAISE(ABORT, 'synthetic receipt transaction fault'); END")

                def mutate():
                    if operation == 'begin':
                        return receipts.begin('logical', 'payload', {})
                    if operation == 'finish':
                        return receipts.finish('logical', physical, 'COMPLETE')
                    return receipts.reconcile('logical', physical, outcome='ERROR', evidence_reference='synthetic')

                with self.assertRaises(IntegrityError) as caught:
                    mutate()
                self.assertIsInstance(caught.exception.orig, sqlite3.IntegrityError)
                self.assertEqual(self.rows(receipts), before)
                with closing(sqlite3.connect(self.path(receipts))) as connection, connection:
                    connection.execute('DROP TRIGGER fail_logical')
                mutate()
                reopened = self.open(operation)
                snapshot = self.rows(reopened)
                self.assertEqual(len(snapshot['receipt_physical']), 1)
                self.assertEqual(list(snapshot['receipt_logical'].values()), list(snapshot['receipt_physical'].values()))

    def test_begin_commit_ack_loss_keeps_started_but_this_transport_invocation_does_not_send(self):
        receipts = self.open()
        transport = self.transport(receipts)
        with self.lost_commit_ack(receipts), self.assertRaises(CommitUnknown):
            _Transport._send_attempt(transport, '/synthetic', {}, {}, 'payload', {'request_id': 'logical'})
        transport._request_response.assert_not_called()
        self.assertEqual(transport._issued_requests, 0)
        reopened = self.open()
        snapshot = self.rows(reopened)
        self.assertEqual(len(snapshot['receipt_physical']), 1)
        self.assertEqual(list(snapshot['receipt_logical'].values()), list(snapshot['receipt_physical'].values()))
        self.assertEqual(next(iter(snapshot['receipt_physical'].values()))['status'], 'STARTED')
        with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
            reopened.begin('logical', 'payload', {})
        self.assertEqual(self.rows(reopened), snapshot)

    def test_finish_commit_ack_loss_keeps_completed_receipt_and_one_observed_send(self):
        receipts = self.open()
        transport = self.transport(receipts)
        metadata = {'request_id': 'logical'}
        with self.lost_commit_ack(receipts, after=2), self.assertRaises(CommitUnknown):
            _Transport._send_attempt(transport, '/synthetic', {}, {}, 'payload', metadata)
        self.assertEqual(transport._request_response.call_count, 1)
        self.assertEqual(transport._issued_requests, 1)
        reopened = self.open()
        snapshot = self.rows(reopened)
        physical = metadata['physical_request_id']
        self.assertEqual(reopened._database.read(physical)['status'], 'COMPLETE')
        self.assertEqual(list(snapshot['receipt_logical'].values()), list(snapshot['receipt_physical'].values()))
        with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_PHYSICAL_REQUEST_TRANSITION'):
            reopened.finish('logical', physical, 'UNKNOWN')
        with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_ALREADY_SETTLED'):
            reopened.begin('logical', 'payload', {})
        self.assertEqual(self.rows(reopened), snapshot)

    def test_wrong_logical_dataset_and_stale_physical_rejections_leave_actual_rows_unchanged(self):
        receipts, different = self.open(), self.open('different-dataset')
        first = receipts.begin('first', 'payload-first', {})
        receipts.begin('second', 'payload-second', {})
        before, other = self.rows(receipts), self.rows(different)
        for selected, logical in ((receipts, 'second'), (different, 'first')):
            with self.subTest(logical=logical), self.assertRaisesRegex(RuntimePolicyError, 'INVALID_PHYSICAL_REQUEST_TRANSITION'):
                selected.finish(logical, first, 'COMPLETE')
            with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_REQUEST_RECONCILIATION'):
                selected.reconcile(logical, first, outcome='ERROR', evidence_reference='synthetic')
            self.assertEqual(self.rows(receipts), before)
            self.assertEqual(self.rows(different), other)
        self.assertIsNone(different._database.read(first))
        receipts.finish('first', first, 'ERROR')
        receipts.begin('first', 'new-payload', {'retry_of': first, 'retry_reason': 'explicit'})
        before = self.rows(receipts)
        with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_PHYSICAL_REQUEST_TRANSITION'):
            receipts.finish('first', first, 'COMPLETE')
        with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_REQUEST_RECONCILIATION'):
            receipts.reconcile('first', first, outcome='COMPLETE', evidence_reference='stale synthetic evidence')
        self.assertEqual(self.rows(receipts), before)

    def test_explicit_reconcile_does_not_refund_independent_unknown_budget(self):
        receipts = self.open()
        physical = receipts.begin('logical', 'payload', {})
        with closing(BudgetLedger(self.root / 'independent-budget.sqlite3', {'calls': 2, 'tokens': 100})) as budget:
            budget.reserve(physical, {'calls': 1, 'tokens': 71})
            budget.finish(physical, status='UNKNOWN')
            receipts.finish('logical', physical, 'UNKNOWN')
            entry, summary = budget.entry(physical), budget.summary()
            before = self.rows(receipts)
            with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
                receipts.begin('logical', 'payload', {'retry_of': physical, 'retry_reason': 'explicit'})
            with self.assertRaisesRegex(ValueError, 'RECONCILIATION_EVIDENCE_REQUIRED'):
                receipts.reconcile('logical', physical, outcome='ERROR', evidence_reference=' ')
            self.assertEqual(self.rows(receipts), before)
            receipts.reconcile('logical', physical, outcome='ERROR', evidence_reference='synthetic operator inspection')
            second = receipts.begin('logical', 'payload', {'retry_of': physical, 'retry_reason': 'explicit'})
            self.assertNotEqual(second, physical)
            self.assertEqual(budget.entry(physical), entry)
            self.assertEqual(budget.summary(), summary)
            self.assertEqual(budget.summary()['effective_usage_or_reserved'], {'calls': 1, 'tokens': 71})

    def test_terminal_retry_policy_requires_link_and_complete_requires_format_correction(self):
        for terminal in ('COMPLETE', 'ERROR'):
            with self.subTest(terminal=terminal):
                receipts = self.open(terminal)
                physical = receipts.begin('logical', 'payload', {})
                receipts.finish('logical', physical, terminal)
                before = self.rows(receipts)
                invalid = ({}, {'retry_of': 'other', 'retry_reason': 'explicit'}, {'retry_of': physical, 'retry_reason': ''})
                if terminal == 'COMPLETE':
                    invalid += ({'retry_of': physical, 'retry_reason': 'explicit'},)
                for metadata in invalid:
                    with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_ALREADY_SETTLED'):
                        receipts.begin('logical', 'payload', metadata)
                    self.assertEqual(self.rows(receipts), before)
                second = receipts.begin('logical', 'new-payload', {'retry_of': physical,
                    'retry_reason': 'format_correction' if terminal == 'COMPLETE' else 'explicit'})
                self.assertNotEqual(second, physical)
                self.assertEqual(receipts._database.read(physical)['status'], terminal)


if __name__ == '__main__':
    unittest.main()
