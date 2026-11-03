from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.database_contracts import SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_providers import sqlite as owner
from .discovered_lifecycle_lint import sqlite_initialization_violations


ROOT = Path(__file__).resolve().parents[2]


def busy(code=sqlite3.SQLITE_BUSY):
    error = sqlite3.OperationalError('synthetic journal admission fault')
    error.sqlite_errorcode = code
    return error


@contextmanager
def scripted_connection(results):
    clock, statements, cursors, sleeps = SimpleNamespace(now=0.0), [], [], []
    outcomes = iter(results)

    def execute(statement):
        statements.append(statement)
        cursor = Mock()
        if statement.startswith('PRAGMA journal_mode'):
            value, seconds = next(outcomes)
            clock.now += seconds
            if isinstance(value, BaseException):
                raise value
            cursor.fetchone.return_value = (value,)
        cursors.append(cursor)
        return cursor

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    connection = Mock(execute=Mock(side_effect=execute))
    with patch.object(owner.time, 'monotonic', side_effect=lambda: clock.now), \
            patch.object(owner.time, 'sleep', side_effect=sleep):
        yield SimpleNamespace(connection=connection, clock=clock, statements=statements,
                              cursors=cursors, sleeps=sleeps)


class SQLiteInitializationContracts(unittest.TestCase):
    def test_busy_read_or_switch_retries_only_unadmitted_journal_configuration(self):
        for responses in ([(busy(), 0), ('wal', 0)], [('delete', 0), (busy(), 0), ('wal', 0)]):
            with self.subTest(responses=len(responses)), scripted_connection(responses) as fixture:
                owner._initialize(fixture.connection, None, wal=True)
                self.assertEqual(fixture.sleeps, [0.01])
                self.assertEqual(fixture.statements.count('PRAGMA synchronous=FULL'), 1)
                self.assertEqual(fixture.statements[-4:], ['PRAGMA busy_timeout=30000',
                    'PRAGMA synchronous=FULL', 'PRAGMA foreign_keys=ON', 'PRAGMA recursive_triggers=ON'])
                self.assertTrue(all(statement.startswith('PRAGMA ') for statement in fixture.statements))
                fixture.connection.commit.assert_not_called()
                fixture.connection.rollback.assert_not_called()
                for cursor in fixture.cursors:
                    cursor.close.assert_called_once()

    def test_read_elapsed_time_reduces_switch_timeout_and_success_restores_connection_policy(self):
        with scripted_connection([('delete', 20), ('wal', 1)]) as fixture:
            owner._initialize(fixture.connection, None, wal=True)
            timeouts = [statement for statement in fixture.statements if statement.startswith('PRAGMA busy_timeout=')]
            self.assertEqual(timeouts, ['PRAGMA busy_timeout=30000', 'PRAGMA busy_timeout=10000',
                                        'PRAGMA busy_timeout=30000'])
            self.assertEqual(fixture.sleeps, [])
            for cursor in fixture.cursors:
                cursor.close.assert_called_once()

    def test_persistent_busy_cannot_reset_deadline_or_admit_connection(self):
        failure = busy()
        with scripted_connection([(failure, 10)] * 3) as fixture:
            with self.assertRaises(sqlite3.OperationalError) as caught:
                owner._initialize(fixture.connection, None, wal=True)
            self.assertIs(caught.exception, failure)
            self.assertEqual(fixture.statements.count('PRAGMA journal_mode'), 3)
            timeouts = [int(statement.split('=')[1]) for statement in fixture.statements
                        if statement.startswith('PRAGMA busy_timeout=')]
            self.assertEqual(timeouts[0], 30000)
            self.assertLess(timeouts[1], 20000)
            self.assertLess(timeouts[2], 10000)
            self.assertNotIn('PRAGMA synchronous=FULL', fixture.statements)
            self.assertEqual(fixture.sleeps, [0.01, 0.01])

    def test_deadline_exhausted_by_read_refuses_switch_before_another_sql_wait(self):
        with scripted_connection([('delete', 30)]) as fixture:
            with self.assertRaisesRegex(TimeoutError, 'SQLITE_JOURNAL_MODE_TIMEOUT'):
                owner._initialize(fixture.connection, None, wal=True)
            self.assertEqual(fixture.statements, ['PRAGMA busy_timeout=30000', 'PRAGMA journal_mode'])
            for cursor in fixture.cursors:
                cursor.close.assert_called_once()

    def test_only_busy_including_extended_busy_is_retryable(self):
        with scripted_connection([(busy(sqlite3.SQLITE_BUSY | (2 << 8)), 0), ('wal', 0)]) as fixture:
            owner._initialize(fixture.connection, None, wal=True)
            self.assertEqual(fixture.sleeps, [0.01])
        for error in (busy(sqlite3.SQLITE_LOCKED), busy(sqlite3.SQLITE_IOERR),
                      sqlite3.OperationalError('no SQLite result code')):
            with self.subTest(error=getattr(error, 'sqlite_errorcode', None)), scripted_connection([(error, 0)]) as fixture:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    owner._initialize(fixture.connection, None, wal=True)
                self.assertIs(caught.exception, error)
                self.assertEqual(fixture.sleeps, [])

    def test_wrong_mode_and_fetch_failure_close_cursors_and_refuse_admission(self):
        with scripted_connection([('delete', 0), ('delete', 0)]) as fixture:
            with self.assertRaisesRegex(RuntimeError, 'SQLITE_WAL_UNAVAILABLE'):
                owner._initialize(fixture.connection, None, wal=True)
            self.assertEqual(fixture.sleeps, [])
            for cursor in fixture.cursors:
                cursor.close.assert_called_once()
        timeout, query = Mock(), Mock()
        query.fetchone.side_effect = busy(sqlite3.SQLITE_IOERR)
        connection = Mock()
        connection.execute.side_effect = [timeout, query]
        with self.assertRaises(sqlite3.OperationalError):
            owner._initialize(connection, None, wal=True)
        timeout.close.assert_called_once()
        query.close.assert_called_once()

    def test_independent_engines_converge_to_wal_on_simultaneous_first_connections(self):
        with TemporaryDirectory() as directory:
            providers = [SQLiteProvider(directory), SQLiteProvider(directory)]
            barrier, initialize = Barrier(4), owner._initialize

            def observed(connection, record, **options):
                barrier.wait(timeout=10)
                initialize(connection, record, **options)

            try:
                with patch.object(owner, '_initialize', side_effect=observed):
                    spaces = [provider.open_space(SpaceKey('bootstrap', 'fixture', 'same-path')) for provider in providers]
                    engines = [engine for space in spaces for engine in (space.runtime_engine, space.migration_engine)]
                    def connect(engine):
                        with engine.connect() as connection:
                            return connection.exec_driver_sql('PRAGMA journal_mode').scalar_one()
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        self.assertEqual(list(pool.map(connect, engines)), ['wal'] * 4)
            finally:
                for provider in providers:
                    provider.close()

    def test_existing_wal_writer_allows_new_reader_without_another_mode_change(self):
        with TemporaryDirectory() as directory:
            first, second = SQLiteProvider(directory), SQLiteProvider(directory)
            key = SpaceKey('bootstrap', 'fixture', 'writer')
            try:
                one, two = first.open_space(key), second.open_space(key)
                with one.migration_engine.connect() as writer:
                    writer.exec_driver_sql('CREATE TABLE fixture(value INTEGER)')
                    writer.commit()
                    writer.exec_driver_sql('BEGIN IMMEDIATE')
                    writer.exec_driver_sql('INSERT INTO fixture VALUES(1)')
                    with two.runtime_engine.connect() as reader:
                        self.assertEqual(reader.exec_driver_sql('PRAGMA journal_mode').scalar_one(), 'wal')
                        self.assertEqual(reader.exec_driver_sql('SELECT count(*) FROM fixture').scalar_one(), 0)
                    writer.rollback()
            finally:
                first.close()
                second.close()

    def test_explicit_legacy_file_stays_delete_without_new_sidecars(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite3'
            key = SpaceKey('bootstrap', 'fixture', 'legacy')
            provider = SQLiteProvider(directory, files={key: path})
            try:
                space = provider.open_space(key)
                for engine in (space.runtime_engine, space.migration_engine):
                    with engine.connect() as connection:
                        self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode').scalar_one(), 'delete')
                        self.assertEqual(connection.exec_driver_sql('PRAGMA synchronous').scalar_one(), 2)
            finally:
                provider.close()
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_reviewed_initialization_and_unsafe_source_mutations(self):
        source = (ROOT / 'kb_platform/database_providers/sqlite.py').read_text('utf-8')
        self.assertEqual(sqlite_initialization_violations(source), [])
        for before, after in (
            ('code & 0xff != sqlite3.SQLITE_BUSY', 'False'),
            ('deadline - time.monotonic()', '30'),
            ("if read('PRAGMA journal_mode') != mode:", 'if True:'),
            ('with closing(connection.execute(statement)) as cursor:', 'with connection.execute(statement) as cursor:'),
            ('time.sleep(min(0.01, remaining))', 'time.sleep(30)'),
            ('PRAGMA synchronous=FULL', 'PRAGMA synchronous=NORMAL'),
            ("{'poolclass': NullPool}", "{'poolclass': QueuePool}"),
        ):
            with self.subTest(mutation=before):
                self.assertIn(before, source)
                self.assertTrue(sqlite_initialization_violations(source.replace(before, after, 1)))
