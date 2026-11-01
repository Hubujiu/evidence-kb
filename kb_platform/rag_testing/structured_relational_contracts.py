from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from time import sleep
import unittest
from unittest.mock import patch

from sqlalchemy import Column, Float, Integer, MetaData, Table, Text, bindparam, column, delete, func, literal, select, table, text, update
from sqlalchemy.exc import StatementError
from sqlalchemy.sql.dml import Update

from kb_platform.database_contracts import DatabaseError, Migration, QueryLimits, SchemaPackage, SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_sdk import ExistingDataset
from kb_platform.storage_sdk.relational import (
    After, Clock, ExactString, GUARDED_RELATIONAL_V1, STRUCTURED_RELATIONAL_V1,
    StructuredSchema, TypedJSON, UtcDateTime,
)


def structured_fixture_schema(owner='consumer'):
    metadata = MetaData()
    records = Table('records', metadata,
        Column('tenant', ExactString(100), primary_key=True), Column('id', ExactString(100), primary_key=True),
        Column('payload', TypedJSON, nullable=False), Column('revision', Integer, nullable=False),
        Column('deadline', UtcDateTime), Column('measure', Float), info={'scope': ('tenant',)})
    audit = Table('audit', metadata, Column('id', Integer, primary_key=True), Column('text', Text, nullable=False))
    return StructuredSchema(owner, metadata, appendonly=('audit',)), records, audit


@contextmanager
def structured_fixture(root, *, owner='consumer', space='main', package=None):
    provider = SqlStorageProvider(SQLiteProvider(root))
    key = SpaceKey('contracts', owner, space)
    identity = provider.initialize('structured', key)
    exported = {item.name: item for item in provider.exports()}['structured']
    store = exported.open_space(key, ExistingDataset(identity.dataset_id))
    selected, records, audit = structured_fixture_schema(owner)
    try:
        store.prepare(selected if package is None else package)
        store.seal()
        yield store, records, audit
    finally:
        provider.close()


class StructuredRelationalContracts(unittest.TestCase):
    def test_independent_capability_has_no_raw_sql_or_backend_information(self):
        self.assertFalse(STRUCTURED_RELATIONAL_V1.satisfies(
            __import__('kb_platform.storage_sdk', fromlist=['RELATIONAL_V1']).RELATIONAL_V1))
        self.assertTrue(GUARDED_RELATIONAL_V1.satisfies(STRUCTURED_RELATIONAL_V1))
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            STRUCTURED_RELATIONAL_V1.validate(store)
            GUARDED_RELATIONAL_V1.validate(store)
            self.assertFalse(hasattr(store, 'info'))
            self.assertFalse(hasattr(store, 'physical_name'))
            with store.transaction(write=False) as tx:
                self.assertEqual(tx.execute(select(literal(1))).scalar(), 1)
                for statement in ('SELECT 1', text('SELECT 1')):
                    with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_EXPRESSION_REQUIRED'):
                        tx.execute(statement)

    def test_json_clock_and_deadline_roundtrip_with_utc_semantics(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            before = datetime.now(timezone.utc)
            with store.transaction(scope={'tenant': 'A'}) as tx:
                tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {'中文': [1, None, True]},
                                    'revision': 1, 'deadline': After(2), 'measure': 1793531644.123456})
                value = tx.execute(select(records)).first()
                clock = tx.execute(select(Clock().label('now'))).scalar()
                self.assertIsNone(tx.execute(select(After(None).label('absent_deadline'))).scalar())
            self.assertEqual(value['payload'], {'中文': [1, None, True]})
            self.assertEqual(value['measure'], 1793531644.123456)
            self.assertEqual(value['deadline'].tzinfo, timezone.utc)
            self.assertGreaterEqual(value['deadline'], before + timedelta(seconds=1))
            self.assertGreater(value['deadline'], clock)
            with store.transaction(scope={'tenant': 'A'}) as tx:
                with self.assertRaises((ValueError, StatementError)):
                    tx.insert(records, {'tenant': 'A', 'id': 'invalid', 'payload': {'number': float('nan')},
                                        'revision': 1})

    def test_trusted_scope_applies_without_domain_predicates(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            for tenant in ('A', 'B', 'a', 'A '):
                with store.transaction(scope={'tenant': tenant}) as tx:
                    tx.insert(records, {'tenant': tenant, 'id': 'x', 'payload': {'tenant': tenant}, 'revision': 1})
            with store.transaction(scope={'tenant': 'A'}) as tx:
                self.assertEqual([row['tenant'] for row in tx.execute(select(records)).rows], ['A'])
                left, right = records.alias('left_records'), records.alias('right_records')
                joined = select(left.c.tenant, right.c.tenant.label('other_tenant')).select_from(
                    left.join(right, left.c.id == right.c.id))
                self.assertEqual(tx.execute(joined).rows, ({'tenant': 'A', 'other_tenant': 'A'},))
                self.assertEqual(tx.execute(update(records).values(revision=2)).rowcount, 1)
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_SCOPE_REPLACEMENT_REJECTED'):
                    tx.execute(update(records).values(tenant='B'))
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_SCOPE_REPLACEMENT_REJECTED'):
                    tx.insert(records, {'tenant': 'B', 'id': 'escape', 'payload': {}, 'revision': 1})
            with store.transaction(scope={'tenant': 'B'}, write=False) as tx:
                self.assertEqual(tx.execute(select(records.c.revision)).scalar(), 1)

    def test_string_functions_with_undefined_cross_provider_unicode_semantics_are_rejected(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction(scope={'tenant': 'A'}, write=False) as tx:
                for function in (func.lower, func.upper, func.length):
                    with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_FUNCTION_UNSUPPORTED'):
                        tx.execute(select(function(records.c.id)))

    def test_numeric_aggregates_reject_text_json_and_utc_arguments_with_undefined_portable_semantics(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction(scope={'tenant': 'A'}) as tx:
                tx.insert(records, {'tenant': 'A', 'id': 'Ab中', 'payload': {}, 'revision': 1})
                self.assertEqual(tx.execute(select(func.max(records.c.revision))).scalar(), 1)
                self.assertEqual(tx.execute(select(func.count(records.c.payload))).scalar(), 1)
                for function in (func.min, func.max, func.sum, func.avg):
                    for argument in (records.c.id, records.c.payload, records.c.deadline):
                        with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_FUNCTION_UNSUPPORTED'):
                            tx.execute(select(function(argument)))
            with store.transaction(write=False) as tx:
                self.assertEqual(tx.execute(select(records)).rows, ())

    def test_emulated_returning_does_not_report_success_after_lease_expires_between_capture_and_update(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction(scope={'tenant': 'A'}) as tx:
                tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {}, 'revision': 1, 'deadline': After(.1)})
                execute = tx._run

                def delayed(statement, params=None):
                    if isinstance(statement, Update):
                        sleep(.15)
                    return execute(statement, params)

                with patch.object(store._space.runtime_engine.dialect, 'update_returning', False), \
                        patch.object(tx, '_run', side_effect=delayed):
                    result = tx.execute(update(records).where(records.c.deadline >= Clock())
                                        .values(revision=2).returning(records.c.revision))
                self.assertEqual(result.rows, ())
                self.assertEqual(result.rowcount, 0)
                self.assertEqual(tx.execute(select(records.c.revision)).scalar(), 1)

    def test_mutation_returning_uses_locked_identity_after_predicate_changes(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction(scope={'tenant': 'A'}) as tx:
                saved = tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {}, 'revision': 1},
                                  returning=(records.c.id, records.c.revision))
                self.assertEqual(saved.rows, ({'id': 'x', 'revision': 1},))
                changed = tx.execute(update(records).where(records.c.revision == bindparam('previous'))
                                     .values(revision=2).returning(records.c.id, records.c.revision), {'previous': 1})
                self.assertEqual(changed.rows, ({'id': 'x', 'revision': 2},))
                removed = tx.execute(delete(records).where(records.c.revision == 2).returning(records.c.id))
                self.assertEqual(removed.rows, ({'id': 'x'},))
                self.assertEqual(tx.execute(select(records)).rows, ())

    def test_mutations_reject_nested_reads_that_could_bypass_the_trusted_scope(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            for tenant, revision in (('A', 1), ('B', 999)):
                with store.transaction(scope={'tenant': tenant}) as tx:
                    tx.insert(records, {'tenant': tenant, 'id': 'x', 'payload': {}, 'revision': revision})
            with store.transaction(scope={'tenant': 'A'}) as tx:
                hidden = select(records.c.revision).where(records.c.tenant == 'B').scalar_subquery()
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_MUTATION_SUBQUERY_UNSUPPORTED'):
                    tx.execute(update(records).values(revision=hidden))
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_MUTATION_SUBQUERY_UNSUPPORTED'):
                    tx.insert(records, {'tenant': 'A', 'id': 'escape', 'payload': {}, 'revision': hidden})
                self.assertEqual(tx.execute(select(records.c.revision)).scalar(), 1)

    def test_lightweight_table_or_unowned_column_cannot_bypass_declared_table_scopes(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            for tenant, revision in (('A', 1), ('B', 999)):
                with store.transaction(scope={'tenant': tenant}) as tx:
                    tx.insert(records, {'tenant': tenant, 'id': 'x', 'payload': {}, 'revision': revision})
            shadow = table('records', column('tenant'), column('revision'))
            with store.transaction(scope={'tenant': 'A'}, write=False) as tx:
                for untrusted in (select(shadow), select(shadow.alias('shadow')),
                                  select(column('revision')).select_from(records)):
                    with self.assertRaises(DatabaseError):
                        tx.execute(untrusted)
                self.assertEqual(tx.execute(select(records.c.revision)).scalar(), 1)

    def test_simultaneous_update_expressions_read_old_values_and_dependency_cycles_are_rejected(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction(scope={'tenant': 'A'}) as tx:
                tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {}, 'revision': 0})
                for before in range(3):
                    tx.execute(update(records).ordered_values(
                        (records.c.revision, records.c.revision + 1), (records.c.measure, records.c.revision)))
                    row = tx.execute(select(records.c.revision, records.c.measure)).first()
                    self.assertEqual((row['revision'], row['measure']), (before + 1, float(before)))
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_UPDATE_DEPENDENCY_UNSUPPORTED'):
                    tx.execute(update(records).values(revision=records.c.measure, measure=records.c.revision))

    def test_upsert_and_rollback_preserve_record_identity(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction(scope={'tenant': 'A'}) as tx:
                tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {}, 'revision': 1})
                ignored = tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {'ignored': True}, 'revision': 9},
                                    conflict_keys=('tenant', 'id'))
                self.assertEqual(ignored.rowcount, 0)
                saved = tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {}, 'revision': 9},
                                  conflict_keys=('tenant', 'id'), updates={'revision': records.c.revision + 1},
                                  returning=(records.c.revision,))
                self.assertEqual(saved.scalar(), 2)
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with store.transaction(scope={'tenant': 'A'}) as tx:
                    tx.execute(update(records).values(revision=100))
                    raise ValueError('rollback')
            with store.transaction(scope={'tenant': 'A'}, write=False) as tx:
                self.assertEqual(tx.execute(select(records.c.revision)).scalar(), 2)

    def test_concurrent_writers_do_not_lose_increment_and_reads_remain_bounded(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction(scope={'tenant': 'A'}) as tx:
                tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {}, 'revision': 0})

            def change(index):
                with store.transaction(scope={'tenant': 'A'}) as tx:
                    current = tx.execute(select(records.c.revision).with_for_update()).scalar()
                    tx.execute(update(records).values(revision=current + 1))

            with ThreadPoolExecutor(max_workers=4) as pool:
                tuple(pool.map(change, range(8)))
            with store.transaction(scope={'tenant': 'A'}, write=False) as tx:
                self.assertEqual(tx.execute(select(records.c.revision)).scalar(), 8)
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_QUERY_LIMIT'):
                with store.transaction(scope={'tenant': 'A'}, write=False,
                                       limits=QueryLimits(max_statements=1)) as tx:
                    tx.execute(select(literal(1)))
                    tx.execute(select(literal(2)))

    def test_appendonly_and_readonly_constraints_are_enforced_at_protocol_boundary(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.transaction() as tx:
                tx.insert(audit, {'id': 1, 'text': 'immutable'})
                self.assertEqual(tx.insert(audit, {'id': 1, 'text': 'ignored'}, conflict_keys=('id',)).rowcount, 0)
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_APPENDONLY'):
                    tx.insert(audit, {'id': 1, 'text': 'changed'}, conflict_keys=('id',), updates={'text': 'changed'})
                for mutation in (update(audit).values(text='changed'), delete(audit)):
                    with self.assertRaisesRegex(DatabaseError, 'DATABASE_APPENDONLY'):
                        tx.execute(mutation)
            with store.transaction(write=False) as tx:
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_WRITE_TRANSACTION_REQUIRED'):
                    tx.execute(select(audit).with_for_update())
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_WRITE_TRANSACTION_REQUIRED'):
                    tx.execute(delete(audit))

    def test_guard_is_separate_from_committing_transactions_and_rejects_concurrent_producer(self):
        with TemporaryDirectory() as root, structured_fixture(root) as (store, records, audit):
            with store.guard('producer'):
                with store.transaction(scope={'tenant': 'A'}) as tx:
                    tx.insert(records, {'tenant': 'A', 'id': 'x', 'payload': {}, 'revision': 1})
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_GUARD_BUSY'):
                    with store.guard('producer'):
                        self.fail('Concurrent guard was granted')
                with store.guard('another-producer'):
                    with store.transaction(scope={'tenant': 'A'}, write=False) as tx:
                        self.assertEqual(tx.execute(select(records.c.revision)).scalar(), 1)
            with store.guard('producer'):
                pass

    def test_legacy_schema_is_rejected_and_new_schema_fingerprint_is_immutable(self):
        with TemporaryDirectory() as root:
            provider = SqlStorageProvider(SQLiteProvider(root))
            key = SpaceKey('contracts', 'consumer', 'legacy')
            identity = provider.initialize('relational', key)
            exports = {item.name: item for item in provider.exports()}
            try:
                raw = exports['relational'].open_space(key, ExistingDataset(identity.dataset_id))
                raw.prepare(SchemaPackage('consumer', (Migration(1, ('CREATE TABLE old_data(id INTEGER PRIMARY KEY)',)),)))
                new = exports['structured'].open_space(key, ExistingDataset(identity.dataset_id))
                package, records, audit = structured_fixture_schema()
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_MIGRATION_REQUIRED'):
                    new.prepare(package)
            finally:
                provider.close()
        with TemporaryDirectory() as root:
            with structured_fixture(root):
                pass
            package, records, audit = structured_fixture_schema()
            records.append_column(Column('extra', Integer))
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_STRUCTURED_SCHEMA_MISMATCH'):
                with structured_fixture(root, package=package):
                    self.fail('Changed schema was silently adopted')
