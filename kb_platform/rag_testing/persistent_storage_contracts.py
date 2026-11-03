from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from psycopg.errors import UndefinedTable
from pymysql.err import ProgrammingError as MySQLProgrammingError

from kb_platform.database_contracts import DatabaseError, Migration, SchemaPackage, SpaceKey
from kb_platform.database_migrations import migration_connection
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.storage_adapters.values import MAX_REVISION, export_documents, import_documents
from kb_platform.storage_sdk import StorageError
from .storage_values_contracts import value_store


def exercise_migration_journal(bound):
    check = unittest.TestCase()
    owner = bound.identity.space.owner_id
    first = Migration(1, ('CREATE TABLE journal_one(id INTEGER PRIMARY KEY)',
                          'INSERT INTO journal_one VALUES(1)'))
    bad = Migration(2, ('CREATE TABLE journal_two(id INTEGER PRIMARY KEY)',
                        'INSERT INTO journal_absent VALUES(1)'))
    package = SchemaPackage(owner, (first, bad))
    mysql = bound.info.engine == 'mysql'
    error_type = {'sqlite': OperationalError, 'postgresql': UndefinedTable,
                  'mysql': MySQLProgrammingError}[bound.info.engine]
    with check.assertRaises(error_type) as error:
        bound.prepare(package)
    if mysql:
        check.assertEqual(error.exception.args[0], 1146)
    elif bound.info.engine == 'postgresql':
        check.assertEqual(error.exception.sqlstate, '42P01')
    with migration_connection(bound._space) as conn:
        rows = [tuple(row) for row in conn.execute(text(
            'SELECT version,dirty,step FROM _database_migrations ORDER BY version'))]
    check.assertEqual(rows, [(1, 0, 2), (2, 1, 1)] if mysql else [])
    if mysql:
        with check.assertRaisesRegex(DatabaseError, 'DATABASE_MIGRATION_DIRTY'):
            bound.prepare(package)
        with check.assertRaisesRegex(DatabaseError, 'DATABASE_SCHEMA_DOWNGRADE'):
            bound.prepare(SchemaPackage(owner, (first,)))
        changed = SchemaPackage(owner, (first, Migration(2, ('SELECT 1',))))
        with check.assertRaisesRegex(DatabaseError, 'DATABASE_MIGRATION_HASH_MISMATCH'):
            bound.prepare(changed)
    else:
        corrected = SchemaPackage(owner, (first, Migration(2, (
            'CREATE TABLE journal_two(id INTEGER PRIMARY KEY)', 'INSERT INTO journal_two VALUES(2)'))))
        check.assertEqual(bound.prepare(corrected), (1, 2))
        check.assertEqual(bound.prepare(corrected), ())
        with migration_connection(bound._space) as conn:
            check.assertEqual([tuple(row) for row in conn.execute(text(
                'SELECT version,dirty,step FROM _database_migrations ORDER BY version'))],
                [(1, 0, 2), (2, 0, 2)])
    return {'engine': bound.info.engine, 'failed_journal': rows, 'dirty_retry': 'refused' if mysql else 'rolled_back'}


def exercise_mysql_release_failure(bound):
    check = unittest.TestCase()
    original = Connection.execute
    def fail_release(connection, statement, *args, **kwargs):
        if str(statement).startswith('SELECT RELEASE_LOCK'):
            raise RuntimeError('synthetic release failure')
        return original(connection, statement, *args, **kwargs)
    with patch.object(Connection, 'execute', fail_release):
        with check.assertRaisesRegex(RuntimeError, 'synthetic release failure'):
            with migration_connection(bound._space):
                pass
    from sqlalchemy import create_engine
    from kb_platform.database_providers.mysql import _connect_args

    engine = create_engine(bound._space.migration_engine.url, connect_args=_connect_args())
    try:
        with engine.connect() as conn:
            name = 'migration_' + bound.identity.space.digest[:48]
            check.assertEqual(conn.execute(text('SELECT GET_LOCK(:name,0)'), {'name': name}).scalar(), 1)
            check.assertEqual(conn.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': name}).scalar(), 1)
    finally:
        engine.dispose()
    return {'engine': 'mysql', 'release_failure': 'discarded', 'independent_lock': 'acquired'}


class PersistentMigrationContracts(unittest.TestCase):
    def test_real_sqlite_rolls_back_all_versions_and_restarts_cleanly(self):
        with TemporaryDirectory() as root:
            service = DatabaseService()
            self.addCleanup(service.close)
            service.register('sqlite', SQLiteProvider(Path(root)))
            bound = service.bind(service.grant('sqlite', SpaceKey('app', 'journal', 'scope'), 'fixture'))
            self.assertEqual(exercise_migration_journal(bound)['failed_journal'], [])

    def test_mysql_release_failure_still_closes_connection(self):
        for failure in ('release', 'release-commit'):
            with self.subTest(failure=failure):
                conn = Mock()
                conn.execute.return_value.scalar.return_value = 1
                if failure == 'release':
                    conn.execute.side_effect = [Mock(scalar=lambda: 1), RuntimeError('release failed')]
                else:
                    conn.commit.side_effect = [None, RuntimeError('release commit failed')]
                space = SimpleNamespace(migration_engine=Mock(connect=lambda: conn),
                                        info=SimpleNamespace(engine='mysql'), key=SimpleNamespace(digest='a' * 64))
                with self.assertRaises(RuntimeError):
                    with migration_connection(space):
                        pass
                conn.close.assert_called_once_with()
                conn.invalidate.assert_called_once_with()

    def test_invalidation_failure_cannot_skip_final_close(self):
        conn = Mock()
        conn.execute.side_effect = [Mock(scalar=lambda: 1), KeyboardInterrupt('release interrupted')]
        conn.invalidate.side_effect = RuntimeError('invalidate failed')
        space = SimpleNamespace(migration_engine=Mock(connect=lambda: conn),
                                info=SimpleNamespace(engine='mysql'), key=SimpleNamespace(digest='a' * 64))
        with self.assertRaisesRegex(KeyboardInterrupt, 'release interrupted'):
            with migration_connection(space):
                pass
        conn.close.assert_called_once_with()

    def test_primary_migration_error_survives_all_secondary_cleanup_errors(self):
        for cause in ('body', 'commit'):
            with self.subTest(cause=cause):
                primary = RuntimeError('primary migration failure')
                conn = Mock()
                conn.execute.side_effect = [Mock(scalar=lambda: 1), RuntimeError('release failed')]
                conn.rollback.side_effect = RuntimeError('rollback failed')
                conn.invalidate.side_effect = RuntimeError('invalidate failed')
                conn.close.side_effect = RuntimeError('close failed')
                if cause == 'commit':
                    conn.commit.side_effect = primary
                space = SimpleNamespace(migration_engine=Mock(connect=lambda: conn),
                    info=SimpleNamespace(engine='mysql'), key=SimpleNamespace(digest='a' * 64))
                with self.assertRaises(RuntimeError) as error:
                    with migration_connection(space):
                        if cause == 'body':
                            raise primary
                self.assertIs(error.exception, primary)
                self.assertEqual(len(primary.__notes__), 4)
                conn.close.assert_called_once_with()
                conn.invalidate.assert_called_once_with()

    def test_grant_failure_does_not_replay_durable_migration(self):
        with TemporaryDirectory() as root:
            service = DatabaseService()
            self.addCleanup(service.close)
            service.register('sqlite', SQLiteProvider(Path(root)))
            bound = service.bind(service.grant('sqlite', SpaceKey('app', 'journal', 'grant'), 'fixture'))
            package = SchemaPackage('journal', (Migration(1, ('CREATE TABLE once_only(id INTEGER)',)),))
            original = bound._space.grant_runtime
            def fail_after_durable_commit(package):
                with bound._space.migration_engine.connect() as conn:
                    self.assertEqual(conn.execute(text('SELECT dirty,step FROM _database_migrations')).one(), (0, 1))
                raise RuntimeError('grant failed')
            with patch.object(bound._space, 'grant_runtime', side_effect=fail_after_durable_commit):
                with self.assertRaisesRegex(RuntimeError, 'grant failed'):
                    bound.prepare(package)
            with patch.object(bound._space, 'grant_runtime', wraps=original) as grant:
                self.assertEqual(bound.prepare(package), ())
                grant.assert_called_once_with(package)


class PersistentDocumentContracts(unittest.TestCase):
    def test_max_revision_never_wraps_live_or_tombstone_and_clone_keeps_limit(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted), TemporaryDirectory() as root, value_store(root) as source:
                source.put('limit', {})
                with source._database.transaction(write=True) as tx:
                    tx.execute('UPDATE kb_storage_documents SET revision=:maximum,deleted=:deleted',
                               {'maximum': MAX_REVISION, 'deleted': int(deleted)})
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                with value_store(root, space='clone') as target:
                    self.assertEqual(import_documents(target, snapshot,
                        expected_source_dataset_id=source.identity.dataset_id,
                        expected_target_dataset_id=target.identity.dataset_id), 0 if deleted else 1)
                    for store in (source, target):
                        for expected in (None, 0, 1, MAX_REVISION):
                            with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                                store.put('limit', {'changed': True}, expected_revision=expected)
                            if deleted:
                                self.assertFalse(store.delete('limit', expected_revision=expected))
                            else:
                                with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                                    store.delete('limit', expected_revision=expected)
                        after = export_documents(store, expected_dataset_id=store.identity.dataset_id)
                        self.assertEqual(after.payload['documents'], snapshot.payload['documents'])
                        self.assertEqual(after.payload['tombstones'], snapshot.payload['tombstones'])
