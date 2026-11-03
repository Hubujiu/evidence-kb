from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import CheckConstraint, Column, Integer, MetaData, Table, func, select
from sqlalchemy.exc import DBAPIError

from kb_platform.database_contracts import DatabaseError, QueryLimits, SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.storage_sdk.relational import StructuredSchema
from kb_platform.storage_structured_wire import PortableStructuredClient, PortableStructuredEndpoint
from .database_foundation_contracts import task_schema
from .storage_structured_wire_contracts import structured


def schema_fixture():
    metadata = MetaData()
    table = Table('records', metadata, Column('id', Integer, primary_key=True))
    return StructuredSchema('records', metadata), table


class StorageSchemaAdmissionContracts(unittest.TestCase):
    def test_changed_same_object_is_not_an_idempotent_prepare_after_seal(self):
        schema, table = schema_fixture()
        original_digest = schema.digest
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema).seal()
            table.append_column(Column('later', Integer))
            self.assertNotEqual(schema.digest, original_digest)
            second = PortableStructuredClient(endpoint.execute, identity=client.identity)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_MISMATCH'):
                second.prepare(schema)
            with endpoint._store._space.migration_engine.connect() as connection:
                columns = connection.exec_driver_sql('PRAGMA table_info(records)').all()
                self.assertEqual([row[1] for row in columns], ['id'])
                self.assertEqual(connection.exec_driver_sql(
                    'SELECT schema_hash FROM _database_structured').scalar(), original_digest)

    def test_unchanged_prepare_ack_retries_before_and_after_seal_do_not_reapply(self):
        schema, table = schema_fixture()
        equivalent, _ = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            with patch.object(endpoint._store, 'prepare', wraps=endpoint._store.prepare) as prepare:
                client.prepare(equivalent)
                client.prepare(equivalent).seal().seal()
                with client.transaction() as tx:
                    tx.insert(table, {'id': 1})
                client.prepare(equivalent)
                self.assertEqual(prepare.call_count, 1)
            with endpoint._store.transaction(write=False) as tx:
                self.assertEqual(tx.execute(select(table)).rows, ({'id': 1},))
            self.assertIs(endpoint._store._package.metadata.tables['records'], table)

    def test_lost_prepare_ack_retries_without_replaying_backend_prepare(self):
        schema, _ = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            def lose_ack(request):
                response = endpoint.execute(request)
                if request['op'] == 'prepare':
                    raise TimeoutError('synthetic lost prepare acknowledgement')
                return response

            uncertain = PortableStructuredClient(lose_ack, identity=client.identity)
            with patch.object(endpoint._store, 'prepare', wraps=endpoint._store.prepare) as prepare:
                with self.assertRaises(TimeoutError):
                    uncertain.prepare(schema)
                client.prepare(schema).seal()
                self.assertEqual(prepare.call_count, 1)

    def test_first_wire_transaction_closes_backend_but_not_idempotent_prepare(self):
        schema, table = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            with patch.object(endpoint._store, 'prepare', wraps=endpoint._store.prepare) as prepare:
                client.prepare(schema)
                with client.transaction(write=False) as tx:
                    self.assertEqual(tx.execute(select(table)).rows, ())
                self.assertFalse(endpoint._sealed)
                self.assertTrue(endpoint._store._sealed)
                client.prepare(schema)
                self.assertEqual(prepare.call_count, 1)
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_PREPARE_CLOSED'):
                endpoint._store.prepare(schema)

    def test_lost_seal_ack_does_not_undo_seal_or_retry_automatically(self):
        schema, _ = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            client.prepare(schema)
            calls = []

            def lose_ack(request):
                response = endpoint.execute(request)
                calls.append(request['op'])
                if request['op'] == 'seal':
                    raise TimeoutError('synthetic lost seal acknowledgement')
                return response

            uncertain = PortableStructuredClient(lose_ack, identity=client.identity)
            with self.assertRaises(TimeoutError):
                uncertain.seal()
            self.assertEqual(calls, ['seal'])
            self.assertTrue(endpoint._sealed)
            self.assertTrue(endpoint._store._sealed)
            client.seal()

    def test_prepare_failure_after_durable_effect_does_not_publish_success(self):
        schema, _ = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            original = endpoint._store.prepare
            failure = RuntimeError('synthetic provider return failure after actual SQLite prepare')

            def failed_return(package):
                original(package)
                raise failure

            with patch.object(endpoint._store, 'prepare', side_effect=failed_return):
                with self.assertRaises(RuntimeError) as caught:
                    client.prepare(schema)
            self.assertIs(caught.exception, failure)
            self.assertIsNone(endpoint._package)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_REQUIRED'):
                client.seal()
            client.prepare(schema).seal()

    def test_real_sqlite_prepare_failure_rolls_back_and_can_be_corrected(self):
        schema, table = schema_fixture()
        invalid = CheckConstraint(func.sum(table.c.id) > 0)
        table.append_constraint(invalid)
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            with self.assertRaises(DBAPIError):
                client.prepare(schema)
            self.assertIsNone(endpoint._package)
            with endpoint._store._space.migration_engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE name IN ('records','_database_structured')").all(), [])
            table.constraints.remove(invalid)
            client.prepare(schema).seal()
            with client.transaction() as tx:
                tx.insert(table, {'id': 2})

    def test_other_declared_digest_cannot_replace_prepared_schema(self):
        schema, _ = schema_fixture()
        other, table = schema_fixture()
        table.append_column(Column('later', Integer))
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, original, _):
            endpoint = PortableStructuredEndpoint(original._store, schemas=(schema, other))
            self.addCleanup(endpoint.close)
            portable = PortableStructuredClient(endpoint.execute, identity=client.identity)
            portable.prepare(schema)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_MISMATCH'):
                portable.prepare(other)

    def test_opaque_equal_digest_does_not_equate_different_schema_owners(self):
        class OpaqueSchema(StructuredSchema):
            @property
            def digest(self):
                return 'opaque-provider-revision'

        original, _ = schema_fixture()
        schema = OpaqueSchema(original.owner, original.metadata)
        other = OpaqueSchema('other', original.metadata)
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, original_endpoint, _):
            endpoint = PortableStructuredEndpoint(original_endpoint._store, schemas=(schema, other))
            self.addCleanup(endpoint.close)
            portable = PortableStructuredClient(endpoint.execute, identity=client.identity)
            portable.prepare(schema)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_MISMATCH'):
                endpoint.execute({'version': 1, 'op': 'prepare', 'owner': other.owner,
                                  'schema_version': other.version, 'digest': other.digest})

    def test_malformed_prepare_and_seal_acknowledgements_are_not_success(self):
        schema, _ = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            for operation in ('prepare', 'seal'):
                for acknowledgement in (None, {}, {'status': 'UNKNOWN'}, {'status': True}):
                    def corrupt_ack(request):
                        response = endpoint.execute(request)
                        return acknowledgement if request['op'] == operation else response

                    portable = PortableStructuredClient(corrupt_ack, identity=client.identity)
                    with self.subTest(operation=operation, acknowledgement=acknowledgement):
                        with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_SCHEMA_INVALID'):
                            getattr(portable, operation)(schema) if operation == 'prepare' else portable.seal()
            client.prepare(schema).seal()

    def test_reopened_store_checks_persisted_hash_and_preserves_rows(self):
        schema, table = schema_fixture()
        with TemporaryDirectory() as directory:
            with structured(directory, schema) as (client, _, _):
                client.prepare(schema).seal()
                with client.transaction() as tx:
                    tx.insert(table, {'id': 4})
            changed, changed_table = schema_fixture()
            changed_table.append_column(Column('later', Integer))
            with structured(directory, changed) as (client, _, _):
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_MISMATCH'):
                    client.prepare(changed)
            with structured(directory, schema) as (client, _, _):
                client.prepare(schema).seal()
                with client.transaction(write=False) as tx:
                    self.assertEqual(tx.execute(select(table)).rows, ({'id': 4},))

    def test_unprepared_wrong_owner_and_closed_requests_do_not_admit(self):
        schema, _ = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            begin = {'version': 1, 'op': 'begin', 'write': False, 'scope': {},
                     'limits': asdict(QueryLimits()), 'operation_id': 'not-prepared'}
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_REQUIRED'):
                endpoint.execute(begin)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_REQUIRED'):
                client.seal()
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_SCHEMA_UNDECLARED'):
                endpoint.execute({'version': 1, 'op': 'prepare', 'owner': 'other',
                                  'schema_version': schema.version, 'digest': schema.digest})
            client.prepare(schema)
            endpoint.close()
            for request in ({'version': 1, 'op': 'schemas'}, {'version': 1, 'op': 'seal'}, begin,
                            {'version': 1, 'op': 'prepare', 'owner': schema.owner,
                             'schema_version': schema.version, 'digest': schema.digest}):
                with self.subTest(op=request['op']), self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_CLOSED'):
                    endpoint.execute(request)

    def test_direct_store_hash_owner_and_first_transaction_boundaries(self):
        schema, table = schema_fixture()
        with TemporaryDirectory() as directory, structured(directory, schema) as (_, endpoint, _):
            store = endpoint._store
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_REQUIRED'):
                store.seal()
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_SCHEMA_OWNER_MISMATCH'):
                store.prepare(StructuredSchema('other', schema.metadata))
            store.prepare(schema)
            changed, changed_table = schema_fixture()
            changed_table.append_column(Column('later', Integer))
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_MISMATCH'):
                store.prepare(changed)
            with self.assertRaisesRegex(ValueError, 'INVALID_DATABASE_TRANSACTION_MODE'):
                with store.transaction(write='yes'):
                    self.fail('invalid mode admitted')
            store.prepare(schema)
            with store.transaction() as tx:
                tx.insert(table, {'id': 3})
            store.seal().seal()
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_PREPARE_CLOSED'):
                store.prepare(schema)

    def test_bound_database_seal_is_per_handle_and_consumer_clone_inherits_snapshot(self):
        with TemporaryDirectory() as directory:
            service = DatabaseService()
            try:
                service.register('main', SQLiteProvider(Path(directory)))
                space = SpaceKey('schema-contract', 'tasks', 'synthetic')
                grant = service.grant('main', space, 'consumer')
                parent = service.bind(grant)
                child = parent.for_consumer({'generation': 1}, nullcontext, consumer_name='consumer')
                child.prepare(task_schema())
                child.seal().seal()
                self.assertEqual(parent.prepare(task_schema()), ())
                self.assertEqual(service.bind(grant).prepare(task_schema()), ())
                inherited = child.for_consumer({'generation': 2}, nullcontext, consumer_name='consumer')
                self.assertEqual(inherited.identity, parent.identity)
                self.assertEqual(inherited.registration_id, parent.registration_id)
                self.assertEqual(inherited._consumer_identity, (('generation', 2),))
                unnamed = child.for_consumer({'generation': 3}, nullcontext)
                self.assertEqual(unnamed.identity, child.identity)
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_PREPARE_CLOSED'):
                    unnamed.prepare(task_schema())
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_PREPARE_CLOSED'):
                    inherited.prepare(task_schema())
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_CONSUMER_GRANT_MISMATCH'):
                    parent.for_consumer({}, nullcontext, consumer_name='other')
                sibling = parent.for_consumer({'generation': 4}, nullcontext, consumer_name='consumer')
                with parent.transaction(write=False) as tx:
                    self.assertEqual(tx.execute('SELECT count(*) FROM tasks').scalar(), 0)
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_PREPARE_CLOSED'):
                    parent.prepare(task_schema())
                self.assertEqual(sibling.prepare(task_schema()), ())
                service.close()
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_ROUTE_UNAVAILABLE'):
                    with inherited.transaction(write=False):
                        self.fail('closed route admitted')
            finally:
                service.close()
