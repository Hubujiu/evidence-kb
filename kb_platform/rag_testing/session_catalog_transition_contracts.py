from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kb_platform.database_contracts import CommitUnknown
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_storage.schema import artifacts, events, requests, sessions
from kb_platform.session_store import SessionStore, canonical, digest


OWNER = Actor('synthetic-tenant', 'synthetic-owner')


class SessionCatalogTransitionContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = self.open()

    def open(self):
        store = SessionStore(self.root)
        self.addCleanup(store.close)
        return store

    def rows(self):
        with self.store.database.transaction(write=False) as transaction:
            return {table.name: [dict(row) for row in transaction.execute(select(table)).rows]
                    for table in (sessions, requests, events, artifacts)}

    @contextmanager
    def commit_fault(self, *, committed):
        transaction = self.store.database.transaction
        injected = False

        @contextmanager
        def fault(*args, **kwargs):
            nonlocal injected
            eligible = kwargs.get('write', True) and not injected
            with transaction(*args, **kwargs) as current:
                yield current
                if eligible and not committed:
                    injected = True
                    raise OSError('synthetic failure before commit')
            if eligible and committed:
                injected = True
                raise CommitUnknown('synthetic-session-commit')

        with patch.object(self.store.database, 'transaction', side_effect=fault):
            yield

    def test_create_failure_before_commit_and_lost_ack_are_distinct_after_reopen(self):
        with self.commit_fault(committed=False), self.assertRaises(OSError):
            self.store.create(OWNER, 'scope', {'value': 1})
        self.assertEqual(self.open().list_sessions(OWNER), [])
        self.assertTrue(all(not rows for rows in self.rows().values()))
        with self.commit_fault(committed=True), self.assertRaises(CommitUnknown):
            self.store.create(OWNER, 'scope', {'value': 2})
        reopened = self.open()
        listed = reopened.list_sessions(OWNER)
        self.assertEqual(len(listed), 1)
        saved = reopened.load(OWNER, listed[0]['session_id'])
        self.assertEqual(saved['revision'], 0)
        self.assertEqual(saved['state'], {'value': 2})
        self.assertEqual(reopened.list_sessions(Actor('other-tenant', OWNER.principal_id)), [])
        self.assertEqual(reopened.list_sessions(Actor(OWNER.tenant_id, 'other-owner')), [])
        self.assertEqual(len(self.rows()['sessions']), 1)

    def test_each_wrong_owner_read_or_mutation_rejects_without_changing_real_tables(self):
        sid = self.store.create(OWNER, 'scope', {})['session_id']
        token = self.store.begin(OWNER, sid, 'turn', 0, 'fingerprint')['token']
        receipt = self.store.artifact(OWNER, sid, token, {'private': 'synthetic'})
        before = self.rows()
        for actor in (Actor(OWNER.tenant_id, 'other-owner'), Actor('other-tenant', OWNER.principal_id)):
            self.assertEqual(self.store.list_sessions(actor), [])
            operations = (
                lambda: self.store.load(actor, sid),
                lambda: self.store.events(actor, sid),
                lambda: self.store.read_artifact(actor, sid, receipt['artifact_id']),
                lambda: self.store.begin(actor, sid, 'turn', 0, 'fingerprint'),
                lambda: self.store.append(actor, sid, token, 'message', {}, 'turn'),
                lambda: self.store.artifact(actor, sid, token, {'forbidden': True}),
                lambda: self.store.finish(actor, sid, token, {}, {'status': 'complete'}),
                lambda: self.store.fail(actor, sid, token, {}, {'status': 'error'}),
            )
            for operation in operations:
                with self.subTest(actor=actor), self.assertRaises(PlatformError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, 'SESSION_NOT_FOUND')
                self.assertEqual(self.rows(), before)

    def test_concurrent_artifact_dedup_is_session_scoped_and_uses_canonical_utf8_content(self):
        sid = self.store.create(OWNER, 'scope', {})['session_id']
        token = self.store.begin(OWNER, sid, 'turn', 0, 'fingerprint')['token']
        second = self.open()
        barrier = Barrier(2, timeout=10)
        body = {'text': '合成证据', 'nested': [1, 2]}

        def put(store):
            barrier.wait()
            return store.artifact(OWNER, sid, token, body)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(put, (self.store, second)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], {'artifact_id': digest(body), 'sha256': digest(body),
                                     'bytes': len(canonical(body).encode())})
        self.assertEqual(len(self.rows()['artifacts']), 1)
        unrelated = self.store.create(OWNER, 'scope', {})['session_id']
        with self.assertRaises(PlatformError) as raised:
            second.read_artifact(OWNER, unrelated, results[0]['artifact_id'])
        self.assertEqual(raised.exception.code, 'ARTIFACT_NOT_FOUND')
        returned = second.read_artifact(OWNER, sid, results[0]['artifact_id'])
        returned['nested'].append(3)
        self.assertEqual(second.read_artifact(OWNER, sid, results[0]['artifact_id']), body)

    def test_artifact_sqlite_failure_rolls_back_and_lost_ack_deduplicates_durable_content(self):
        sid = self.store.create(OWNER, 'scope', {})['session_id']
        token = self.store.begin(OWNER, sid, 'turn', 0, 'fingerprint')['token']
        body = {'evidence': 'synthetic'}
        before = self.rows()
        with closing(sqlite3.connect(self.store.database._space.physical_name)) as connection, connection:
            connection.execute("CREATE TRIGGER synthetic_artifact_failure AFTER INSERT ON artifacts "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic artifact write failure'); END")
        with self.assertRaises(IntegrityError):
            self.store.artifact(OWNER, sid, token, body)
        self.assertEqual(self.rows(), before)
        with closing(sqlite3.connect(self.store.database._space.physical_name)) as connection, connection:
            connection.execute('DROP TRIGGER synthetic_artifact_failure')
        with self.commit_fault(committed=True), self.assertRaises(CommitUnknown):
            self.store.artifact(OWNER, sid, token, body)
        durable = self.rows()
        self.assertEqual(len(durable['artifacts']), 1)
        reopened = self.open()
        receipt = reopened.artifact(OWNER, sid, token, body)
        self.assertEqual(reopened.read_artifact(OWNER, sid, receipt['artifact_id']), body)
        self.assertEqual(self.rows(), durable)

    def test_appendonly_rejects_mutation_and_seeded_malformed_content_fails_integrity(self):
        sid = self.store.create(OWNER, 'scope', {})['session_id']
        token = self.store.begin(OWNER, sid, 'turn', 0, 'fingerprint')['token']
        body = {'evidence': 'synthetic original'}
        receipt = self.store.artifact(OWNER, sid, token, body)
        original = self.rows()
        with closing(sqlite3.connect(self.store.database._space.physical_name)) as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'DATABASE_APPENDONLY'):
                connection.execute('UPDATE artifacts SET body=? WHERE session=? AND id=?',
                                   (canonical({'corrupted': True}), sid, receipt['artifact_id']))
        self.assertEqual(self.rows(), original)
        body = {'evidence': 'synthetic malformed initial fixture'}
        receipt = {'artifact_id': digest(body)}
        with self.store.database.transaction() as transaction:
            transaction.insert(artifacts, {'session': sid, 'id': receipt['artifact_id'],
                'body': canonical({'mismatched': True}), 'created': 'synthetic'})
        before = self.rows()
        for operation in (
            lambda: self.store.read_artifact(OWNER, sid, receipt['artifact_id']),
            lambda: self.store.artifact(OWNER, sid, token, body),
        ):
            with self.assertRaises(PlatformError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 'ARTIFACT_INTEGRITY')
            self.assertEqual(self.rows(), before)

    def test_artifact_expired_writer_rejection_preserves_tables(self):
        sid = self.store.create(OWNER, 'scope', {})['session_id']
        with patch('kb_platform.session_store.time.time', return_value=100):
            token = self.store.begin(OWNER, sid, 'turn', 0, 'fingerprint', lease_seconds=1)['token']
        before = self.rows()
        with patch('kb_platform.session_store.time.time', return_value=102), self.assertRaises(PlatformError) as raised:
            self.store.artifact(OWNER, sid, token, {'late': True})
        self.assertEqual(raised.exception.code, 'TURN_LEASE_EXPIRED')
        self.assertEqual(self.rows(), before)


if __name__ == '__main__':
    unittest.main()
