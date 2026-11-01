from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_store import SessionStore, digest
from kb_platform.session_transfer import (TABLE_COLUMNS, export_sessions, export_legacy_sqlite,
                                          import_sessions, validate_export)


def exercise_session_database(first, second):
    check = unittest.TestCase()
    actor = Actor('synthetic-tenant', 'synthetic-principal')
    sid = first.create(actor, 'scope', {'value': 1})['session_id']
    barrier = Barrier(2, timeout=15)

    def begin(store, request):
        barrier.wait()
        try:
            return store.begin(actor, sid, request, 0, request)
        except PlatformError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(begin, first, 'A'), pool.submit(begin, second, 'B')
        outcomes = [a.result(timeout=35), b.result(timeout=35)]
    winners = [result for result in outcomes if isinstance(result, dict)]
    check.assertEqual(len(winners), 1, outcomes)
    check.assertEqual([result for result in outcomes if isinstance(result, str)], ['TURN_IN_PROGRESS'])
    token = winners[0]['token']
    artifact = first.artifact(actor, sid, token, {'evidence': '原文'})
    check.assertEqual(second.artifact(actor, sid, token, {'evidence': '原文'}), artifact)
    first.append(actor, sid, token, 'message', {'role': 'user', 'content': 'question'}, 'group')
    before = second.events(actor, sid)
    with check.assertRaises(TypeError):
        first.finish(actor, sid, token, {'invalid': object()}, {'status': 'complete'},
                     assistant_event={'role': 'assistant', 'content': 'must roll back'})
    check.assertEqual(second.load(actor, sid)['revision'], 0)
    check.assertEqual(second.events(actor, sid), before)
    result = first.finish(actor, sid, token, {'value': 2}, {'status': 'complete', 'answer': 'yes'},
                          assistant_event={'role': 'assistant', 'content': 'yes'})
    request = 'A' if isinstance(outcomes[0], dict) else 'B'
    check.assertEqual(second.begin(actor, sid, request, 0, request), {'cached': result})
    check.assertEqual(second.load(actor, sid)['state'], {'value': 2})
    check.assertEqual([event['seq'] for event in second.events(actor, sid)], [1, 2, 3])
    for wrong_actor in (Actor(actor.tenant_id, 'other'), Actor('other', actor.principal_id)):
        check.assertEqual(second.list_sessions(wrong_actor), [])
        for operation in (
            lambda: second.load(wrong_actor, sid), lambda: second.events(wrong_actor, sid),
            lambda: second.read_artifact(wrong_actor, sid, artifact['artifact_id']),
            lambda: second.begin(wrong_actor, sid, request, 0, request),
        ):
            with check.assertRaises(PlatformError) as error:
                operation()
            check.assertEqual(error.exception.code, 'SESSION_NOT_FOUND')
    for table in ('events', 'artifacts'):
        for sql in (f"UPDATE {table} SET session=:sid WHERE session=:sid",
                    f"DELETE FROM {table} WHERE session=:sid"):
            with check.assertRaises(Exception):
                with first.database.transaction(write=True) as tx:
                    tx.execute(sql, {'sid': sid})
        columns = TABLE_COLUMNS[table]
        with first.database.transaction(write=False) as tx:
            frozen = tx.execute(f'SELECT * FROM {table} WHERE session=:sid', {'sid': sid}).fetchall()
        check.assertTrue(frozen)
        replacement = {**frozen[0], ('payload' if table == 'events' else 'body'): '{"forged":true}'}
        fields, values = ','.join(columns), ','.join(':' + column for column in columns)
        insert = f'INSERT INTO {table} ({fields}) VALUES ({values})'
        field = 'payload' if table == 'events' else 'body'
        key = 'session,seq' if table == 'events' else 'session,id'
        engine = first.database.info.engine
        if engine == 'mysql':
            attempts = [insert.replace('INSERT INTO', 'REPLACE INTO', 1),
                        insert + f' ON DUPLICATE KEY UPDATE {field}=VALUES({field})']
        else:
            attempts = [insert + f' ON CONFLICT ({key}) DO UPDATE SET {field}=excluded.{field}']
            if engine == 'sqlite':
                attempts.append(insert.replace('INSERT INTO', 'INSERT OR REPLACE INTO', 1))
        for statement in attempts:
            with check.assertRaises(Exception):
                with first.database.transaction(write=True) as tx:
                    tx.execute(statement, replacement)
            with first.database.transaction(write=False) as tx:
                actual = tx.execute(f'SELECT * FROM {table} WHERE session=:sid', {'sid': sid}).fetchall()
            check.assertEqual(actual, frozen)
    check.assertEqual(second.read_artifact(actor, sid, artifact['artifact_id']), {'evidence': '原文'})
    for value in ('Case', 'case', 'case '):
        current = second.load(actor, sid)
        lease = first.begin(actor, sid, value, current['revision'], value)
        first.finish(actor, sid, lease['token'], current['state'], {'status': 'complete', 'request': value})
    for value in ('Case', 'case', 'case '):
        check.assertEqual(second.begin(actor, sid, value, 0, value)['cached']['request'], value)
    with check.assertRaises(PlatformError) as error:
        first.begin(actor, sid, 'new', 0, 'new')
    check.assertEqual(error.exception.code, 'SESSION_REVISION_CONFLICT')
    with patch('kb_platform.session_store.time.time', return_value=1000):
        expired = first.begin(actor, sid, 'expired', 4, 'expired', lease_seconds=1)
    with patch('kb_platform.session_store.time.time', return_value=1002):
        replacement = second.begin(actor, sid, 'replacement', 4, 'replacement')
        for operation in (
            lambda: first.append(actor, sid, expired['token'], 'message', {}, 'old'),
            lambda: first.finish(actor, sid, expired['token'], {}, {'status': 'complete'}),
            lambda: first.fail(actor, sid, expired['token'], {}, {'status': 'error'}),
        ):
            with check.assertRaises(PlatformError) as error:
                operation()
            check.assertEqual(error.exception.code, 'TURN_LEASE_EXPIRED')
        second.finish(actor, sid, replacement['token'], {'value': 3}, {'status': 'complete'})
    return {'engine': first.database.info.engine, 'concurrency': 'one-writer',
            'session_id': sid, 'revision': second.load(actor, sid)['revision'],
            'events': len(second.events(actor, sid)), 'immutability': 'events-and-artifacts',
            'replacement_cannot_rewrite_history': True,
            'identity_comparison': 'case-and-trailing-space-exact'}


class SessionDatabaseContracts(unittest.TestCase):
    def test_real_sqlite_two_clients_obey_cross_database_contract(self):
        with TemporaryDirectory() as directory:
            first, second = SessionStore(directory), SessionStore(directory)
            try:
                result = exercise_session_database(first, second)
                self.assertEqual(result['engine'], 'sqlite')
                self.assertEqual(result['revision'], 5)
            finally:
                first.close()
                second.close()

    def test_clone_preserves_all_records_responses_and_is_explicit(self):
        with TemporaryDirectory() as source_dir, TemporaryDirectory() as target_dir:
            source, target = SessionStore(source_dir), SessionStore(target_dir)
            try:
                actor = Actor('tenant', 'owner')
                sid = source.create(actor, 'scope', {'original': True})['session_id']
                token = source.begin(actor, sid, 'request', 0, 'fingerprint')['token']
                artifact = source.artifact(actor, sid, token, {'original': '原文'})
                response = source.finish(actor, sid, token, {'done': True}, {'status': 'complete'})
                with self.assertRaisesRegex(ValueError, 'SOURCE_MUST_BE_FROZEN'):
                    export_sessions(source)
                package = export_sessions(source, source_frozen=True)
                with self.assertRaisesRegex(ValueError, 'TARGET_MUST_BE_OFFLINE'):
                    import_sessions(target, package)
                result = import_sessions(target, package, target_offline=True)
                self.assertNotEqual(result['source_dataset_id'], result['target_dataset_id'])
                self.assertEqual(target.begin(actor, sid, 'request', 0, 'fingerprint'), {'cached': response})
                self.assertEqual(target.read_artifact(actor, sid, artifact['artifact_id']), {'original': '原文'})
                self.assertEqual(export_sessions(target, source_frozen=True)['tables'], package['tables'])
                with self.assertRaisesRegex(ValueError, 'TARGET_NOT_EMPTY'):
                    import_sessions(target, package, target_offline=True)
                broken = deepcopy(package)
                broken['tables']['sessions'][0]['revision'] = 500
                with self.assertRaisesRegex(ValueError, 'INTEGRITY'):
                    validate_export(broken)
            finally:
                source.close()
                target.close()

    def test_legacy_path_is_never_silently_ignored(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / 'sessions.sqlite3').write_bytes(b'old database')
            with self.assertRaisesRegex(ValueError, 'LEGACY_SESSION_DATABASE_REQUIRES_EXPLICIT_IMPORT'):
                SessionStore(directory)

    def test_legacy_export_is_readonly_and_unknown_request_is_preserved(self):
        with TemporaryDirectory() as source_dir, TemporaryDirectory() as target_dir:
            source, target = SessionStore(source_dir), SessionStore(target_dir)
            try:
                actor = Actor('tenant', 'owner')
                sid = source.create(actor, 'scope', {'original': True})['session_id']
                source.begin(actor, sid, 'unknown-request', 0, 'fingerprint')
                tables = export_sessions(source, source_frozen=True)['tables']
                path = Path(source_dir) / 'legacy.sqlite3'
                with closing(sqlite3.connect(path)) as connection:
                    for table, columns in TABLE_COLUMNS.items():
                        definitions = ','.join(column + (' INTEGER' if column in {'revision', 'seq'}
                                                         else ' REAL' if column == 'expires' else ' TEXT')
                                               for column in columns)
                        connection.execute(f'CREATE TABLE {table} ({definitions})')
                        for row in tables[table]:
                            connection.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})",
                                               tuple(row[column] for column in columns))
                    connection.commit()
                before = path.read_bytes()
                package = export_legacy_sqlite(path, source_frozen=True)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(package['tables'], tables)
                import_sessions(target, package, target_offline=True)
                with self.assertRaises(PlatformError) as error:
                    target.begin(actor, sid, 'unknown-request', 0, 'fingerprint')
                self.assertEqual(error.exception.code, 'TURN_IN_PROGRESS')
            finally:
                source.close()
                target.close()

    def test_rehashed_export_with_inconsistent_revision_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            try:
                store.create(Actor('tenant', 'owner'), 'scope', {})
                package = export_sessions(store, source_frozen=True)
                package['tables']['sessions'][0]['revision'] = 7
                package['record_hashes']['sessions'] = [digest(package['tables']['sessions'][0])]
                package['sha256'] = digest({key: value for key, value in package.items() if key != 'sha256'})
                with self.assertRaisesRegex(ValueError, 'SESSION_EXPORT_REVISION'):
                    validate_export(package)
            finally:
                store.close()
