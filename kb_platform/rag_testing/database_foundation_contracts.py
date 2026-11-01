from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, DatabaseError as DriverDatabaseError

from kb_platform.database_contracts import (
    CommitUnknown, DatabaseError, Migration, QueryLimits, SchemaPackage, SpaceKey, SqlStatement,
)
from kb_platform.database_plugins import compile_database_plugins, require_database
from kb_platform.database_process import decode_value, encode_value
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.plugin_host import PluginHost, PluginSpec


def task_schema(owner='tasks'):
    return SchemaPackage(owner, (Migration(1, ('CREATE TABLE tasks(id VARCHAR(64) PRIMARY KEY,value TEXT NOT NULL)',)),))


def exercise_transaction_atomicity(database):
    check = unittest.TestCase()
    probes = (
        ('plain-commit', 'COMMIT'), ('formfeed-commit', '\fCOMMIT'),
        ('formfeed-rollback', '\fROLLBACK'), ('comment-control', '/* leading */\fCOMMIT'),
        ('mysql-comment-control', '# harmless\nCOMMIT'),
        ('mysql-executable-comment', '/*!50000 COMMIT */ # harmless'),
        ('create', 'CREATE TABLE atomicity_ddl(id INTEGER)'),
        ('temporary-ddl', 'CREATE TEMPORARY TABLE atomicity_temp(id INTEGER)'),
        ('alter', 'ALTER TABLE tasks ADD COLUMN forbidden INTEGER'),
        ('truncate', 'TRUNCATE TABLE tasks'), ('grant', 'GRANT SELECT ON tasks TO PUBLIC'),
        ('multi-commit', 'SELECT 1; COMMIT'), ('multi-ddl', 'SELECT 1; CREATE TABLE atomicity_multi(id INTEGER)'),
    )
    observations = []
    for mode in ('execute', 'batch_json'):
        for label, sql in probes:
            key = uuid4().hex
            completed, code = False, None
            try:
                if mode == 'execute':
                    with database.transaction() as tx:
                        tx.execute('INSERT INTO tasks(id,value) VALUES(:id,:value)', {'id': key, 'value': label})
                        tx.execute(sql)
                        completed = True
                        raise RuntimeError('FORCE_TRANSACTION_ROLLBACK')
                else:
                    database.batch_json({'operation_id': uuid4().hex, 'statements': [
                        {'sql': 'INSERT INTO tasks(id,value) VALUES(:id,:value)', 'params': {'id': key, 'value': label}},
                        {'sql': sql},
                        {'sql': "UPDATE tasks SET value='fail' WHERE id='absent'", 'expected_rows': 1},
                    ]})
                    completed = True
            except Exception as error:
                code = getattr(error, 'code', type(error).__name__)
            with database.transaction(write=False) as tx:
                persisted = tx.execute('SELECT count(*) FROM tasks WHERE id=:id', {'id': key}).scalar()
            check.assertFalse(completed, f'{database.info.engine}/{mode}/{label}: command ran inside runtime transaction')
            check.assertNotEqual(code, 'DATABASE_ROWCOUNT_CONFLICT', f'{mode}/{label}: control command reached driver')
            check.assertEqual(persisted, 0, f'{database.info.engine}/{mode}/{label}: failed transaction committed a write')
            observations.append({'engine': database.info.engine, 'mode': mode, 'probe': label,
                                 'persisted_writes': persisted, 'error': code})
    with database.transaction(write=False) as tx:
        check.assertEqual(tx.execute("/* ordinary */ -- comment\n SELECT ';literal;' AS value").scalar(), ';literal;')
    return observations


def exercise_mysql_transactional_schema(database):
    check = unittest.TestCase()
    check.assertEqual(database.info.engine, 'mysql')
    package = SchemaPackage(database.identity.space.owner_id, (Migration(1, (
        'CREATE TABLE tasks(id VARCHAR(64) PRIMARY KEY,value TEXT NOT NULL) ENGINE=MyISAM',
    )),))
    with check.assertRaisesRegex(DatabaseError, 'DATABASE_NONTRANSACTIONAL_TABLE'):
        database.prepare(package)
    with check.assertRaises(DriverDatabaseError):
        with database.transaction() as tx:
            tx.execute("INSERT INTO tasks VALUES('forbidden','must not become ready')")
    return {'engine': 'mysql', 'nontransactional_schema': 'REJECTED',
            'error': 'DATABASE_NONTRANSACTIONAL_TABLE', 'runtime_write': 'DENIED'}


class DatabaseFoundationContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.service = DatabaseService()
        self.service.register('main', SQLiteProvider(self.root))
        self.addCleanup(self.service.close)

    def bind(self, application='project-a', owner='tasks', space='records', *, service=None):
        service = self.service if service is None else service
        return service.bind(service.grant('main', SpaceKey(application, owner, space), 'fixture'))

    def prepared(self, **kwargs):
        bound = self.bind(**kwargs)
        bound.prepare(task_schema(bound.identity.space.owner_id))
        return bound.seal()

    def count(self, bound):
        with bound.transaction(write=False) as tx:
            return tx.execute('SELECT count(*) AS count FROM tasks').scalar()

    def test_identical_plugin_tables_are_isolated_between_projects_and_survive_restart(self):
        first, second = self.prepared(), self.prepared(application='project-b')
        with first.transaction() as tx:
            tx.execute('INSERT INTO tasks VALUES(:id,:value)', {'id': 'same', 'value': 'a'})
        with second.transaction() as tx:
            tx.execute('INSERT INTO tasks VALUES(:id,:value)', {'id': 'same', 'value': 'b'})
        original = first.identity
        self.assertNotEqual(first.identity.dataset_id, second.identity.dataset_id)
        with first.transaction(write=False) as tx:
            self.assertEqual(tx.execute('SELECT value FROM tasks').scalar(), 'a')
        with second.transaction(write=False) as tx:
            self.assertEqual(tx.execute('SELECT value FROM tasks').scalar(), 'b')
        self.service.close()
        replacement = DatabaseService()
        self.addCleanup(replacement.close)
        replacement.register('main', SQLiteProvider(self.root))
        restored = self.prepared(service=replacement)
        self.assertEqual(restored.identity, original)
        self.assertEqual(self.count(restored), 1)
        self.assertEqual(len(list(self.root.glob('*/space.sqlite3'))), 2)

    def test_runtime_connection_denies_schema_escape_security_changes_and_metadata(self):
        bound = self.prepared()
        statements = ('CREATE TABLE forbidden(id INTEGER)', 'DROP TABLE tasks',
                      "ATTACH DATABASE ':memory:' AS external", 'PRAGMA foreign_keys=OFF',
                      'UPDATE _database_identity SET dataset_id=\'forged\'',
                      'SELECT * FROM _database_migrations', "SELECT load_extension('missing')")
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises((DriverDatabaseError, DatabaseError)):
                with bound.transaction() as tx:
                    tx.execute(sql)
        with self.assertRaises(DriverDatabaseError), bound.transaction(write=False) as tx:
            tx.execute("INSERT INTO tasks VALUES('forbidden','write')")
        self.assertEqual(self.count(bound), 0)

    def test_sql_cannot_commit_a_batch_early_or_reuse_a_closed_transaction(self):
        bound = self.prepared()
        for command in ('COMMIT', ';COMMIT;', '-- a\nEND;', '/* a */ROLLBACK', 'BEGIN', 'SAVEPOINT x'):
            with self.subTest(command=command), self.assertRaises(DatabaseError):
                with bound.transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('pending','a')")
                    tx.execute(command)
            self.assertEqual(self.count(bound), 0)
        with bound.transaction(write=False) as tx:
            tx.execute('SELECT 1')
        with self.assertRaisesRegex(DatabaseError, 'TRANSACTION_CLOSED'):
            tx.execute('SELECT 1')

    def test_runtime_statements_cannot_escape_transaction_atomicity(self):
        observations = exercise_transaction_atomicity(self.prepared())
        self.assertEqual(len(observations), 26)

    def test_sqlite_authorizer_denies_runtime_transaction_controls_below_statement_filter(self):
        bound = self.prepared()
        for sql in ('\fCOMMIT', '\fROLLBACK', 'SAVEPOINT escape'):
            with self.subTest(sql=sql), self.assertRaises(DriverDatabaseError):
                with bound.transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('pending','value')")
                    tx._connection.exec_driver_sql(sql)
            self.assertEqual(self.count(bound), 0)
        with bound.transaction() as tx:
            tx.execute("INSERT INTO tasks VALUES('committed','value')")
        self.assertEqual(self.count(bound), 1)

    def test_appendonly_and_readonly_policies_are_enforced_by_sqlite_authorizer(self):
        bound = self.bind()
        package = SchemaPackage('tasks', (Migration(1, (
            'CREATE TABLE tasks(id TEXT PRIMARY KEY,value TEXT NOT NULL)',
            'CREATE TABLE frozen(id TEXT PRIMARY KEY)', "INSERT INTO frozen VALUES('fixed')",
        )),), runtime_readonly=('frozen',), runtime_appendonly=('tasks',))
        bound.prepare(package)
        bound.seal()
        with bound.transaction() as tx:
            tx.execute("INSERT INTO tasks VALUES('x','value')")
        for sql in ("UPDATE tasks SET value='changed'", 'DELETE FROM tasks',
                    "INSERT INTO frozen VALUES('other')", 'DELETE FROM frozen'):
            with self.subTest(sql=sql), self.assertRaises(DriverDatabaseError), bound.transaction() as tx:
                tx.execute(sql)
        self.assertEqual(self.count(bound), 1)

    def test_readonly_table_prefix_stays_protected_when_a_new_provider_prepares_existing_space(self):
        package = SchemaPackage('tasks', (Migration(1, (
            'CREATE TABLE g_first(id VARCHAR(64) PRIMARY KEY,value TEXT)',
            "INSERT INTO g_first VALUES('fixed','original')",
            'CREATE TABLE g_second(id VARCHAR(64) PRIMARY KEY,value TEXT)',
            "INSERT INTO g_second VALUES('fixed','other')",
            'CREATE TABLE tasks(id VARCHAR(64) PRIMARY KEY,value TEXT)',
        )),), runtime_readonly_prefixes=('g_',))
        bound = self.bind()
        bound.prepare(package)
        bound.seal()
        identity = bound.identity
        self.service.close()
        replacement = DatabaseService()
        self.addCleanup(replacement.close)
        replacement.register('main', SQLiteProvider(self.root))
        restored = self.bind(service=replacement)
        self.assertEqual(restored.prepare(package), ())
        restored.seal()
        self.assertEqual(restored.identity, identity)
        for table in ('g_first', 'g_second'):
            for statement in (f"UPDATE {table} SET value='changed'", f'DELETE FROM {table}',
                              f"INSERT OR REPLACE INTO {table} VALUES('fixed','changed')"):
                with self.subTest(statement=statement), self.assertRaises((DriverDatabaseError, DatabaseError)):
                    with restored.transaction() as tx:
                        tx.execute(statement)
        with restored.transaction() as tx:
            tx.execute("INSERT INTO tasks VALUES('allowed','new task')")
        with restored.transaction(write=False) as tx:
            self.assertEqual(tx.execute('SELECT value FROM g_first').scalar(), 'original')
            self.assertEqual(tx.execute('SELECT count(*) FROM tasks').scalar(), 1)

    def test_sqlite_appendonly_rejects_replace_variants_without_requiring_business_triggers(self):
        bound = self.bind()
        package = SchemaPackage('tasks', task_schema().migrations, runtime_appendonly=('tasks',))
        bound.prepare(package)
        self.assertEqual(bound.prepare(package), ())
        bound.seal()
        with bound.transaction(write=False) as tx:
            self.assertEqual(tx.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").scalar(), 2)
        with bound.transaction() as tx:
            tx.execute("INSERT INTO tasks VALUES('fixed','original')")
        for prefix in ('REPLACE', 'INSERT OR REPLACE', 'INSERT /* a */ OR /* b */ REPLACE',
                       'INSERT\nOR\nREPLACE', 'INSERT OR ROLLBACK', 'INSERT OR /* comment */ ROLLBACK'):
            statement = prefix + " INTO tasks VALUES('fixed','changed')"
            with self.subTest(statement=statement), self.assertRaisesRegex(DatabaseError, 'RUNTIME_STATEMENT_REQUIRED'):
                with bound.transaction() as tx:
                    tx.execute(statement)
        replacement = "WITH harmless AS (SELECT 1) INSERT OR REPLACE INTO tasks VALUES('fixed','changed')"
        with self.assertRaises(DriverDatabaseError):
            with bound.transaction() as tx:
                tx.execute(replacement)
        with self.assertRaisesRegex(DatabaseError, 'DATABASE_BATCH_ROLLED_BACK'):
            bound.batch_json({'operation_id': 'appendonly-cte-rollback', 'statements': [
                {'sql': "INSERT INTO tasks VALUES('rolled-back','must disappear')"},
                {'sql': replacement},
            ]})
        with bound.transaction() as tx:
            tx.execute("INSERT OR IGNORE INTO tasks VALUES('fixed','ignored')")
            tx.execute('INSERT INTO tasks VALUES(:id,:value)', {'id': 'new', 'value': 'INSERT OR REPLACE is data'})
        with bound.transaction(write=False) as tx:
            self.assertEqual(tx.execute("SELECT value FROM tasks WHERE id='fixed'").scalar(), 'original')
            self.assertEqual(tx.execute('SELECT count(*) FROM tasks').scalar(), 2)

    def test_schema_hashes_ownership_downgrades_and_prepare_phase_are_checked(self):
        bound = self.bind()
        self.assertEqual(bound.prepare(task_schema()), (1,))
        self.assertEqual(bound.prepare(task_schema()), ())
        changed = SchemaPackage('tasks', (Migration(1, ('CREATE TABLE other(id INTEGER)',)),))
        with self.assertRaisesRegex(DatabaseError, 'HASH_MISMATCH'):
            bound.prepare(changed)
        with self.assertRaisesRegex(DatabaseError, 'OWNER_MISMATCH'):
            bound.prepare(task_schema('other-owner'))
        upgraded = SchemaPackage('tasks', (*task_schema().migrations,
                                          Migration(2, ('ALTER TABLE tasks ADD COLUMN status TEXT',))))
        bound.prepare(upgraded)
        with self.assertRaisesRegex(DatabaseError, 'DOWNGRADE'):
            bound.prepare(task_schema())
        bound.seal()
        with self.assertRaisesRegex(DatabaseError, 'PREPARE_CLOSED'):
            bound.prepare(upgraded)

    def test_failed_transactional_migration_can_be_corrected_without_partial_ddl(self):
        bound = self.bind()
        failed = SchemaPackage('tasks', (Migration(1, ('CREATE TABLE pending(id INTEGER)',
                                                       'INSERT INTO absent VALUES(1)')),))
        with self.assertRaises(DriverDatabaseError):
            bound.prepare(failed)
        self.assertEqual(bound.prepare(task_schema()), (1,))
        with bound.transaction(write=False) as tx:
            self.assertIsNone(tx.execute("SELECT name FROM sqlite_master WHERE name='pending'").first())

    def test_concurrent_services_share_migration_lock_and_dataset_identity(self):
        other = DatabaseService()
        self.addCleanup(other.close)
        other.register('main', SQLiteProvider(self.root))
        start = Event()

        def migrate(service):
            start.wait(5)
            bound = self.bind(service=service)
            applied = bound.prepare(task_schema())
            return applied, bound.identity.dataset_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(migrate, service) for service in (self.service, other)]
            start.set()
            results = [future.result(10) for future in futures]
        self.assertEqual(sorted(result[0] for result in results), [(), (1,)])
        self.assertEqual(results[0][1], results[1][1])

    def test_expected_dataset_never_initializes_a_new_location_or_accepts_a_different_copy(self):
        bound = self.prepared()
        key = bound.identity.space
        for expected in ('wrong-dataset',):
            grant = self.service.grant('main', key, 'fixture', expected_dataset_id=expected)
            with self.assertRaisesRegex(DatabaseError, 'IDENTITY_MISMATCH'):
                self.service.bind(grant)
        missing = self.service.grant('main', SpaceKey('new-project', 'tasks', 'records'), 'fixture',
                                     expected_dataset_id=bound.identity.dataset_id)
        with self.assertRaisesRegex(DatabaseError, 'DATASET_MISSING'):
            self.service.bind(missing)

    def test_forged_grants_and_old_route_bindings_are_rejected(self):
        grant = self.service.grant('main', SpaceKey('project-a', 'tasks', 'records'), 'fixture')
        with self.assertRaisesRegex(DatabaseError, 'GRANT_REJECTED'):
            self.service.bind(replace(grant, space=SpaceKey('project-b', 'tasks', 'records')))
        bound = self.service.bind(grant)
        bound.prepare(task_schema())
        self.service._dispose('main', self.service._routes['main'])
        self.service.register('main', SQLiteProvider(self.root))
        with self.assertRaisesRegex(DatabaseError, 'BINDING_CHANGED'):
            with bound.transaction() as tx:
                tx.execute('SELECT 1')

    def test_drain_retains_inflight_transaction_and_leases_until_commit(self):
        owned = []

        @contextmanager
        def owner():
            owned.append('acquire')
            try:
                yield
            finally:
                owned.append('release')

        release = self.service.register('other', SQLiteProvider(self.root / 'other'), owner_lease=owner)
        grant = self.service.grant('other', SpaceKey('project-a', 'tasks', 'records'), 'fixture')
        bound = self.service.bind(grant)
        bound.prepare(task_schema())
        with bound.transaction() as tx:
            tx.execute("INSERT INTO tasks VALUES('x','value')")
            with self.assertRaisesRegex(DatabaseError, 'ROUTE_BUSY'):
                release()
            self.assertEqual(owned[-1], 'acquire')
            with self.assertRaisesRegex(DatabaseError, 'ROUTE_UNAVAILABLE'):
                with bound.transaction():
                    pass
            self.assertEqual(self.service.inspect()['other']['active_calls'], 1)
        self.assertEqual(owned[-1], 'release')
        release()
        self.assertNotIn('other', self.service.catalog())

    def test_batch_row_conflict_and_result_limits_roll_back_every_write(self):
        bound = self.prepared()
        with self.assertRaisesRegex(DatabaseError, 'ROWCOUNT_CONFLICT'):
            bound.batch((SqlStatement("INSERT INTO tasks VALUES('a','value')"),
                         SqlStatement("UPDATE tasks SET value='changed' WHERE id='missing'", expected_rows=1)))
        self.assertEqual(self.count(bound), 0)
        with self.assertRaisesRegex(DatabaseError, 'RESULT_LIMIT'):
            with bound.transaction(limits=QueryLimits(max_rows=1)) as tx:
                tx.execute("INSERT INTO tasks VALUES('a','value')")
                tx.execute('SELECT 1 UNION ALL SELECT 2')
        self.assertEqual(self.count(bound), 0)

    def test_commit_acknowledgement_loss_is_unknown_and_write_is_not_repeated(self):
        bound = self.prepared()
        actual_commit = Connection.commit
        observed = []

        def lose_ack(connection):
            observed.append('commit')
            actual_commit(connection)
            raise DBAPIError('COMMIT', None, OSError('lost'), connection_invalidated=True)

        with patch.object(Connection, 'commit', lose_ack):
            with self.assertRaises(CommitUnknown) as outcome:
                with bound.transaction(operation_id='physical-001') as tx:
                    tx.execute("INSERT INTO tasks VALUES('once','value')")
        self.assertEqual(outcome.exception.operation_id, 'physical-001')
        self.assertEqual(observed, ['commit'])
        self.assertEqual(self.count(bound), 1)

    def test_typed_process_values_are_lossless_and_query_batches_are_atomic_and_bounded(self):
        values = [None, True, '文本', 2 ** 100, -12, 0.25, Decimal('3.1415926535897932384626'),
                  b'\x00\xff', datetime(2026, 11, 1, tzinfo=timezone.utc)]
        for value in values:
            self.assertEqual(decode_value(encode_value(value)), value)
        for invalid in (123, 1.5, {'type': 'float', 'value': 'nan'}, {'type': 'bytes', 'value': 'invalid!'}):
            with self.subTest(value=invalid), self.assertRaises(DatabaseError):
                decode_value(invalid)
        bound = self.prepared()
        request = {'operation_id': 'batch-1', 'statements': [
            {'sql': 'INSERT INTO tasks VALUES(:id,:value)', 'params': {'id': 'a', 'value': 'first'}, 'expected_rows': 1},
            {'sql': 'UPDATE tasks SET value=:value WHERE id=:id', 'params': {'id': 'a', 'value': 'done'}, 'expected_rows': 1},
        ]}
        self.assertEqual(bound.batch_json(request)['status'], 'COMMITTED')
        result = bound.query_json({'statement': {'sql': 'SELECT value,length(value) AS size FROM tasks'}})
        self.assertEqual(result['rows'], [{'value': 'done', 'size': {'type': 'integer', 'value': '4'}}])
        with self.assertRaisesRegex(DatabaseError, 'QUERY_FAILED'):
            bound.query_json({'statement': {'sql': "DELETE FROM tasks"}})
        with self.assertRaisesRegex(DatabaseError, 'QUERY_LIMIT'):
            bound.query_json({'statement': {'sql': 'SELECT 1'}, 'limits': {'max_rows': 1001}})
        self.assertEqual(self.count(bound), 1)


class DatabasePluginContracts(unittest.TestCase):
    def configuration(self, root):
        return {'version': 1, 'application_id': 'deployment',
                'routes': {'main': {'provider': 'sqlite', 'root': str(root)}},
                'bindings': {name: {'primary': {'route': 'main', 'owner': 'tasks', 'space': 'records',
                                                'application_id': name}}
                             for name in ('project-a', 'project-b')}}

    def fixture(self):
        def mount(ctx, config):
            database = require_database(ctx, 'primary')
            database.prepare(task_schema())
            database.seal()
            ctx.provide(config['service'], database)

        return (PluginSpec('fixture.tasks.a', ('tasks.a',), (), mount, storage_slots=('primary',)),
                PluginSpec('fixture.tasks.b', ('tasks.b',), (), mount, storage_slots=('primary',)))

    def profile(self):
        return {'version': 1, 'plugins': [
            {'id': 'project-a', 'use': 'fixture.tasks.a', 'config': {'service': 'tasks.a'}},
            {'id': 'project-b', 'use': 'fixture.tasks.b', 'config': {'service': 'tasks.b'}},
        ]}

    def test_compiler_builds_explicit_route_binding_consumer_dependencies_without_external_effects(self):
        with TemporaryDirectory() as root:
            profile, catalog = compile_database_plugins(self.profile(), self.fixture(), self.configuration(root),
                slots={'fixture.tasks.a': ('primary',), 'fixture.tasks.b': ('primary',)})
            self.assertEqual(list(Path(root).iterdir()), [])
            specs = {spec.key: spec for spec in catalog}
            for row in profile['plugins']:
                spec = specs[row['use']]
                if row['id'] in ('project-a', 'project-b'):
                    self.assertTrue(any(key.startswith('databases.bound.') for key in spec.requires))
            routes = [spec for spec in catalog if spec.key.startswith('databases.route.')]
            self.assertEqual(len(routes), 1)
            self.assertEqual(routes[0].requires, ('databases.registry',))

    def test_catalog_storage_slots_are_used_without_an_override_mapping(self):
        with TemporaryDirectory() as root:
            profile, catalog = compile_database_plugins(self.profile(), self.fixture(), self.configuration(root))
            with PluginHost(profile, catalog) as host:
                first, second = host.service('tasks.a'), host.service('tasks.b')
                self.assertNotEqual(first.identity.dataset_id, second.identity.dataset_id)
            for mutation in ('missing', 'unknown'):
                configuration = self.configuration(root)
                if mutation == 'missing':
                    del configuration['bindings']['project-a']
                else:
                    configuration['bindings']['project-a']['other'] = {}
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    compile_database_plugins(self.profile(), self.fixture(), configuration)

    def test_catalog_rejects_duplicate_and_empty_storage_slots(self):
        for slots in (('primary', 'primary'), ('',), ('primary', None)):
            with self.subTest(slots=slots), self.assertRaisesRegex(ValueError, 'INVALID_PLUGIN_STORAGE_SLOTS'):
                PluginSpec('fixture.invalid', (), (), lambda ctx, config: None, storage_slots=slots)

    def test_unknown_or_missing_slots_and_routes_are_rejected_before_files_are_created(self):
        with TemporaryDirectory() as root:
            for transform in ('missing', 'unknown', 'unknown-route'):
                configuration = self.configuration(root)
                if transform == 'missing':
                    del configuration['bindings']['project-a']
                elif transform == 'unknown':
                    configuration['bindings']['project-a']['unexpected'] = {}
                else:
                    configuration['bindings']['project-a']['primary']['route'] = 'missing'
                with self.subTest(transform=transform), self.assertRaises(ValueError):
                    compile_database_plugins(self.profile(), self.fixture(), configuration,
                        slots={'fixture.tasks.a': ('primary',), 'fixture.tasks.b': ('primary',)})
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_real_plugins_share_provider_and_keep_independent_tables_across_host_restart(self):
        with TemporaryDirectory() as root:
            configuration = self.configuration(root)
            profile, catalog = compile_database_plugins(self.profile(), self.fixture(), configuration,
                slots={'fixture.tasks.a': ('primary',), 'fixture.tasks.b': ('primary',)})
            with PluginHost(profile, catalog) as host:
                first, second = host.service('tasks.a'), host.service('tasks.b')
                identities = first.identity, second.identity
                with first.transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('same','a')")
                with second.transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('same','b')")
            with PluginHost(profile, catalog) as host:
                first, second = host.service('tasks.a'), host.service('tasks.b')
                self.assertEqual((first.identity, second.identity), identities)
                for database, expected in ((first, 'a'), (second, 'b')):
                    with database.transaction(write=False) as tx:
                        self.assertEqual(tx.execute('SELECT value FROM tasks').scalar(), expected)
            with self.assertRaises(Exception):
                with first.transaction():
                    pass
