from contextlib import contextmanager
from datetime import date, datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import Column, MetaData, String, Table, inspect, select

from ..database_contracts import DatabaseError, StorageIdentity
from ..database_migrations import migration_connection
from .structured import StructuredSqlSpace, _Text


_RECEIPT_METADATA = MetaData()
_RECEIPTS = Table('_database_transfer_receipts', _RECEIPT_METADATA,
    Column('source_sha256', String(64), primary_key=True),
    Column('receipt_json', _Text(), nullable=False))


def _json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('TRANSFER_DATETIME_REQUIRES_TIMEZONE')
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ValueError('TRANSFER_VALUE_UNSUPPORTED')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, default=_json_value)


def content_hash(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def _identity(connection, expected, namespace=None):
    table = Table('_database_identity', MetaData(), schema=namespace,
                  autoload_with=connection, resolve_fks=False)
    rows = connection.execute(select(table)).mappings().all()
    if (len(rows) != 1 or rows[0]['identity_key'] != 1
            or rows[0]['space_key'] != expected.space.key or rows[0]['dataset_id'] != expected.dataset_id):
        raise DatabaseError('TRANSFER_DATASET_IDENTITY_MISMATCH')


@contextmanager
def _source_connection(engine):
    with engine.connect().execution_options(isolation_level='SERIALIZABLE') as connection:
        with connection.begin():
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN')
            elif connection.dialect.name == 'postgresql':
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                if not connection.exec_driver_sql('SELECT rolsuper OR rolbypassrls FROM pg_roles '
                                                  'WHERE rolname=current_user').scalar():
                    raise DatabaseError('TRANSFER_ADMIN_READER_REQUIRED')
                connection.exec_driver_sql('SET LOCAL row_security=off')
            elif connection.dialect.name == 'mysql':
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            yield connection


def read_sql_snapshot(engine, *, namespace, expected_identity, columns, source_frozen):
    if source_frozen is not True:
        raise ValueError('TRANSFER_SOURCE_MUST_BE_FROZEN')
    if not isinstance(expected_identity, StorageIdentity):
        raise ValueError('TRANSFER_EXPECTED_IDENTITY_REQUIRED')
    with _source_connection(engine) as connection:
        _identity(connection, expected_identity, namespace)
        names = set(inspect(connection).get_table_names(schema=namespace))
        if not set(columns) <= names:
            raise DatabaseError('TRANSFER_SOURCE_TABLES_MISSING')
        tables = {}
        for name, fields in columns.items():
            table = Table(name, MetaData(), schema=namespace, autoload_with=connection, resolve_fks=False)
            if set(table.c.keys()) != set(fields):
                raise DatabaseError('TRANSFER_SOURCE_COLUMNS_MISMATCH')
            tables[name] = [dict(row) for row in connection.execute(select(table)).mappings()]
        migrations = Table('_database_migrations', MetaData(), schema=namespace,
                           autoload_with=connection, resolve_fks=False)
        records = [dict(row) for row in connection.execute(select(migrations).order_by(
            migrations.c.schema_owner, migrations.c.version)).mappings()]
        if any(row['dirty'] for row in records):
            raise DatabaseError('TRANSFER_SOURCE_MIGRATION_DIRTY')
        return {'tables': tables, 'migrations': records}


def _rows(connection, table):
    primary = tuple(table.primary_key)
    return [dict(row) for row in connection.execute(select(table).order_by(*primary)).mappings()]


def import_structured_snapshot(target, tables, *, expected_identity, source_sha256,
                               receipt, target_offline):
    if target_offline is not True:
        raise ValueError('TRANSFER_TARGET_MUST_BE_OFFLINE')
    if (not isinstance(target, StructuredSqlSpace) or target.identity != expected_identity
            or target._package is None):
        raise DatabaseError('TRANSFER_INITIALIZED_SQL_TARGET_REQUIRED')
    if set(tables) != set(target._metadata.tables):
        raise ValueError('TRANSFER_TARGET_TABLES_MISMATCH')
    expected = {}
    for table in target._metadata.sorted_tables:
        rows = tables[table.name]
        if any(set(row) != set(table.c.keys()) for row in rows):
            raise ValueError('TRANSFER_TARGET_COLUMNS_MISMATCH')
        keys = tuple(column.name for column in table.primary_key)
        expected[table.name] = sorted(rows, key=lambda row: tuple(row[key] for key in keys))
    serialized = canonical(receipt)
    with migration_connection(target._space) as connection:
        _identity(connection, expected_identity)
        _RECEIPTS.create(connection, checkfirst=True)
    with migration_connection(target._space) as connection:
        _identity(connection, expected_identity)
        previous = connection.execute(select(_RECEIPTS.c.receipt_json).where(
            _RECEIPTS.c.source_sha256 == source_sha256)).scalar()
        if previous is not None and previous != serialized:
            raise DatabaseError('TRANSFER_RECEIPT_CONFLICT')
        actual = {table.name: _rows(connection, table) for table in target._metadata.sorted_tables}
        if previous is not None:
            if content_hash(actual) != content_hash(expected):
                raise DatabaseError('TRANSFER_TARGET_CHANGED')
            return True
        if any(actual.values()):
            raise DatabaseError('TRANSFER_TARGET_NOT_EMPTY')
        for table in target._metadata.sorted_tables:
            for row in expected[table.name]:
                connection.execute(table.insert().values(row))
        actual = {table.name: _rows(connection, table) for table in target._metadata.sorted_tables}
        if content_hash(actual) != content_hash(expected):
            raise DatabaseError('TRANSFER_READBACK_MISMATCH')
        connection.execute(_RECEIPTS.insert().values(source_sha256=source_sha256, receipt_json=serialized))
    return False
