from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from kb_platform.database_contracts import DatabaseError
from kb_platform.database_providers.sqlite_guard import sqlite_guard
from kb_platform.provider_runtime import RequestReceipts, RuntimePolicyError


class ReceiptTransitionContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.receipts = RequestReceipts(self.root)

    def read(self, physical):
        return json.loads((self.root / (physical + '.json')).read_text('utf-8'))

    def retry(self, physical):
        return {'retry_of': physical, 'retry_reason': 'explicit'}

    def test_two_clients_admit_only_one_physical_identity(self):
        barrier = Barrier(2, timeout=5)

        def begin():
            barrier.wait()
            try:
                return RequestReceipts(self.root).begin('logical', 'payload', {})
            except RuntimePolicyError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: begin(), range(2)))
        self.assertEqual(results.count('PROVIDER_REQUEST_UNRESOLVED'), 1)
        self.assertEqual(len(list(self.root.glob('*.json'))), 1)

    def test_finish_and_reconcile_cannot_read_or_publish_over_a_locked_transition(self):
        for competing in ('finish', 'reconcile'):
            with self.subTest(competing=competing), TemporaryDirectory() as directory:
                receipts = RequestReceipts(directory)
                competitor = RequestReceipts(directory)
                physical = receipts.begin('logical', 'payload', {})
                entered, release, attempted = Event(), Event(), Event()
                original_write = receipts._write

                def paused_write(identity, row):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('RECEIPT_BARRIER_TIMEOUT')
                    return original_write(identity, row)

                def compete():
                    attempted.set()
                    if competing == 'finish':
                        competitor.finish('logical', physical, 'UNKNOWN')
                    else:
                        competitor.reconcile('logical', physical, outcome='ERROR', evidence_reference='synthetic')

                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(
                        receipts, '_write', side_effect=paused_write):
                    first = pool.submit(receipts.finish, 'logical', physical, 'COMPLETE')
                    try:
                        self.assertTrue(entered.wait(5))
                        with self.assertRaisesRegex(DatabaseError, 'DATABASE_GUARD_BUSY'):
                            with sqlite_guard(receipts._pending('logical').with_suffix('.guard.sqlite3')):
                                self.fail('competing SQLite connection acquired held guard')
                        second = pool.submit(compete)
                        self.assertTrue(attempted.wait(5))
                    finally:
                        release.set()
                    first.result(timeout=5)
                    with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_'):
                        second.result(timeout=5)
                row = json.loads((Path(directory) / (physical + '.json')).read_text('utf-8'))
                self.assertEqual(row['status'], 'COMPLETE')
                next_id = competitor.begin('logical', 'payload', {
                    'retry_of': physical, 'retry_reason': 'format_correction'})
                with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_PHYSICAL_REQUEST_TRANSITION'):
                    receipts.finish('logical', physical, 'ERROR')
                self.assertEqual(json.loads(receipts._pending('logical').read_text('utf-8'))[
                    'physical_request_id'], next_id)
                with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
                    receipts.begin('logical', 'payload', self.retry(physical))

    def test_all_file_publications_run_inside_matching_logical_guard(self):
        held, observed = set(), []
        original_write = self.receipts._write_path

        @contextmanager
        def observe_guard(path, *, nowait):
            with sqlite_guard(path, nowait=nowait):
                held.add(Path(path))
                try:
                    yield
                finally:
                    held.remove(Path(path))

        def observe_write(target, row):
            self.assertIn(self.receipts._pending(row['logical_request_id']).with_suffix('.guard.sqlite3'), held)
            observed.append(target.suffix)
            original_write(target, row)

        with patch('kb_platform.provider_runtime.sqlite_guard', observe_guard), patch.object(
                self.receipts, '_write_path', side_effect=observe_write):
            first = self.receipts.begin('logical', 'payload', {})
            self.receipts.finish('logical', first, 'UNKNOWN')
            self.receipts.reconcile('logical', first, outcome='ERROR', evidence_reference='synthetic')
            second = self.receipts.begin('logical', 'payload', self.retry(first))
            self.receipts.finish('logical', second, 'COMPLETE')
        self.assertEqual(observed.count('.json'), 5)
        self.assertEqual(observed.count('.terminal'), 2)
        self.assertEqual(held, set())

    def test_finish_requires_pending_physical_owner_and_preserves_both_records(self):
        first = self.receipts.begin('logical', 'payload', {})
        second = self.receipts.begin('different', 'payload', {})
        pending = self.receipts._pending('logical')
        pending.write_bytes(self.receipts._pending('different').read_bytes())
        before = {path: path.read_bytes() for path in self.root.glob('*.json')}
        marker = pending.read_bytes()
        with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_PHYSICAL_REQUEST_TRANSITION'):
            self.receipts.finish('logical', first, 'COMPLETE')
        self.assertEqual(pending.read_bytes(), marker)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.read(second)['status'], 'STARTED')

    def test_unknown_restart_remains_pending_until_explicit_reconciliation(self):
        physical = self.receipts.begin('logical', 'payload', {})
        self.receipts.finish('logical', physical, 'UNKNOWN', diagnostic='synthetic interruption')
        reopened = RequestReceipts(self.root)
        with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
            reopened.begin('logical', 'payload', self.retry(physical))
        self.assertEqual(self.read(physical)['status'], 'UNKNOWN')
        reopened.reconcile('logical', physical, outcome='ERROR', evidence_reference='synthetic reviewed outcome')
        retry = reopened.begin('logical', 'payload', self.retry(physical))
        self.assertNotEqual(retry, physical)
        self.assertEqual(self.read(physical)['diagnostic'], 'synthetic interruption')

    def test_interrupted_publication_preserves_pending_without_automatic_replay(self):
        physical = self.receipts.begin('logical', 'payload', {})
        original_write = self.receipts._write_path

        def fail_terminal(target, row):
            if target.suffix == '.terminal':
                raise OSError('synthetic publication interruption')
            original_write(target, row)

        with patch.object(self.receipts, '_write_path', side_effect=fail_terminal):
            with self.assertRaisesRegex(OSError, 'synthetic publication interruption'):
                self.receipts.finish('logical', physical, 'COMPLETE')
        self.assertEqual(self.read(physical)['status'], 'COMPLETE')
        reopened = RequestReceipts(self.root)
        with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
            reopened.begin('logical', 'payload', self.retry(physical))
        self.assertTrue(reopened._pending('logical').exists())
        with sqlite_guard(reopened._pending('logical').with_suffix('.guard.sqlite3')):
            self.assertTrue(reopened._pending('logical').exists())
