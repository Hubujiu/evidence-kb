from collections.abc import Mapping
from contextlib import contextmanager
from hashlib import sha256
from datetime import timezone
import math
from pathlib import Path
import sqlite3

from sqlalchemy import Column, DateTime, Double, Float, Integer, MetaData, Numeric, String, Table, Text, and_, inspect, literal, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import BindParameter, ColumnClause, TextClause
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import Select, TableClause
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.schema import Table as SchemaTable
from sqlalchemy.sql.schema import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.types import TypeDecorator

from ..database_contracts import DatabaseError, Migration, QueryResult, SchemaPackage, SqlStatement
from ..database_migrations import migration_connection, prepare
from ..storage_sdk.relational import After, Clock, ExactString, IDENTIFIER, StructuredSchema, UtcDateTime


_SAFE_FUNCTIONS = frozenset({'count', 'sum', 'min', 'max', 'avg', 'coalesce'})
_LOCK_METADATA = MetaData()
_WRITE_LOCK = Table('_storage_structured_lock', _LOCK_METADATA, Column('lock_id', Integer, primary_key=True))


class _ExactString(ExactString):
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'mysql':
            from sqlalchemy.dialects.mysql import VARBINARY

            return dialect.type_descriptor(VARBINARY(self.length * 4))
        return dialect.type_descriptor(String(self.length))

    def process_bind_param(self, value, dialect):
        value = super().process_bind_param(value, dialect)
        return value.encode('utf-8') if value is not None and dialect.name == 'mysql' else value

    def process_result_value(self, value, dialect):
        return value.decode('utf-8') if isinstance(value, bytes) else value


class _UtcDateTime(UtcDateTime):
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'mysql':
            from sqlalchemy.dialects.mysql import DATETIME

            return dialect.type_descriptor(DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=dialect.name == 'postgresql'))

    def process_bind_param(self, value, dialect):
        value = super().process_bind_param(value, dialect)
        return value if value is None or dialect.name == 'postgresql' else value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        value = super().process_result_value(value, dialect)
        return None if value is None else value.astimezone(timezone.utc)


class _Text(TypeDecorator):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'mysql':
            from sqlalchemy.dialects.mysql import LONGTEXT

            return dialect.type_descriptor(LONGTEXT())
        return dialect.type_descriptor(Text())


class _Float(TypeDecorator):
    impl = Float
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Double())


def _storage_type(value):
    if isinstance(value, ExactString):
        return _ExactString(value.length)
    if isinstance(value, UtcDateTime):
        return _UtcDateTime()
    if isinstance(value, Text):
        return _Text()
    if isinstance(value, Float):
        return _Float()
    return value


def _physical_metadata(package):
    metadata = MetaData()
    for table in package.metadata.tables.values():
        physical = table.to_metadata(metadata)
        def constraint_key(constraint):
            extra = (str(constraint.sqltext) if isinstance(constraint, CheckConstraint) else
                     tuple(element.target_fullname for element in constraint.elements)
                     if isinstance(constraint, ForeignKeyConstraint) else None)
            return type(constraint), constraint.name, tuple(column.name for column in constraint.columns), extra

        originals = {constraint_key(constraint): constraint for constraint in table.constraints}
        for constraint in physical.constraints:
            constraint._creation_order = originals[constraint_key(constraint)]._creation_order
        for column in physical.columns:
            column.type = _storage_type(column.type)
    return metadata


def _ordered_update(statement):
    assignments = tuple(getattr(statement, '_ordered_values', None) or (statement._values or {}).items())
    by_name = {key.name if hasattr(key, 'name') else key: (key, value) for key, value in assignments}
    dependencies = {name: set() for name in by_name}
    for target, (_, value) in by_name.items():
        for element in visitors.iterate(value):
            if isinstance(element, ColumnClause) and element.table is statement.table:
                if element.name in by_name and element.name != target:
                    dependencies[element.name].add(target)
    ordered = []
    while dependencies:
        ready = [name for name in by_name if name in dependencies and not dependencies[name]]
        if not ready:
            raise DatabaseError('STORAGE_STRUCTURED_UPDATE_DEPENDENCY_UNSUPPORTED')
        for name in ready:
            ordered.append(by_name[name])
            dependencies.pop(name)
        for remaining in dependencies.values():
            remaining.difference_update(ready)
    result = statement._generate()
    result._values = None
    result._maintain_values_ordering = False
    if hasattr(result, '_ordered_values'):
        result._ordered_values = None
    return result.ordered_values(*ordered)


@compiles(Clock, 'postgresql')
def _clock_postgres(element, compiler, **kw):
    return 'clock_timestamp()'


@compiles(Clock, 'mysql')
def _clock_mysql(element, compiler, **kw):
    return 'UTC_TIMESTAMP(6)'


@compiles(Clock, 'sqlite')
def _clock_sqlite(element, compiler, **kw):
    return "strftime('%Y-%m-%d %H:%M:%f','now')"


def _after_value(element, compiler, **kw):
    clauses = tuple(element.clauses)
    if len(clauses) != 1:
        raise ValueError('STORAGE_INVALID_DURATION')
    return compiler.process(clauses[0], **kw)


@compiles(After, 'postgresql')
def _after_postgres(element, compiler, **kw):
    return "(clock_timestamp()+CAST(" + _after_value(element, compiler, **kw) + " AS DOUBLE PRECISION)*INTERVAL '1 second')"


@compiles(After, 'mysql')
def _after_mysql(element, compiler, **kw):
    return 'TIMESTAMPADD(MICROSECOND,CAST((' + _after_value(element, compiler, **kw) + ')*1000000 AS SIGNED),UTC_TIMESTAMP(6))'


@compiles(After, 'sqlite')
def _after_sqlite(element, compiler, **kw):
    return "strftime('%Y-%m-%d %H:%M:%f','now',(" + _after_value(element, compiler, **kw) + ") || ' seconds')"


def _schema_package(space, package, metadata):
    dialect = space.migration_engine.dialect
    quote = dialect.identifier_preparer.quote
    statements = [
        'CREATE TABLE _database_structured (version INTEGER PRIMARY KEY, schema_hash VARCHAR(64) NOT NULL)',
        "INSERT INTO _database_structured VALUES (1,'" + package.digest + "')",
        str(CreateTable(_WRITE_LOCK).compile(dialect=dialect)),
        'INSERT INTO _storage_structured_lock(lock_id) VALUES(1)',
    ]
    for table in metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        statements.extend(str(CreateIndex(index).compile(dialect=dialect))
                          for index in sorted(table.indexes, key=lambda value: value.name))
        scope = tuple(table.info.get('scope', ()))
        if dialect.name == 'postgresql' and scope:
            name = quote(table.name)
            predicate = ' AND '.join(quote(column) + "=current_setting('storage_scope." + column + "',true)"
                                     for column in scope)
            statements.extend((
                'ALTER TABLE ' + name + ' ENABLE ROW LEVEL SECURITY',
                'CREATE POLICY storage_scope ON ' + name + ' USING (' + predicate + ') WITH CHECK (' + predicate + ')',
            ))
    return SchemaPackage(package.owner, (Migration(1, tuple(statements)),), runtime_appendonly=package.appendonly)


class StructuredSqlSpace:
    def __init__(self, space):
        self._space, self._package, self._scopes, self._sealed = space, None, {}, False
        self._metadata = None
        self.identity = space.identity

    def prepare(self, package):
        if self._sealed or not isinstance(package, StructuredSchema):
            raise DatabaseError('DATABASE_PREPARE_CLOSED')
        if package.owner != self.identity.space.owner_id:
            raise DatabaseError('DATABASE_SCHEMA_OWNER_MISMATCH')
        with migration_connection(self._space) as connection:
            inspector = inspect(connection)
            schema = None if connection.dialect.name == 'sqlite' else self._space.physical_name
            names = set(inspector.get_table_names(schema=schema))
            if '_database_structured' not in names:
                if names - {'_database_identity', '_database_migrations'}:
                    raise DatabaseError('STORAGE_STRUCTURED_MIGRATION_REQUIRED')
            else:
                record = connection.exec_driver_sql('SELECT version,schema_hash FROM _database_structured').first()
                if record is None or tuple(record) != (package.version, package.digest):
                    raise DatabaseError('STORAGE_STRUCTURED_SCHEMA_MISMATCH')
        metadata = _physical_metadata(package)
        prepare(self._space, _schema_package(self._space, package, metadata))
        self._package = package
        self._metadata = metadata
        self._scopes = {table.name: tuple(table.info.get('scope', ()))
                        for table in package.metadata.tables.values()}
        return self

    def seal(self):
        if self._package is None:
            raise DatabaseError('STORAGE_STRUCTURED_SCHEMA_REQUIRED')
        self._sealed = True
        return self

    @contextmanager
    def guard(self, name, *, nowait=True):
        if not isinstance(name, str) or not name or len(name.encode('utf-8')) > 512 or type(nowait) is not bool:
            raise ValueError('INVALID_STORAGE_GUARD')
        key = sha256((self.identity.space.key + '\x00' + self.identity.dataset_id + '\x00' + name).encode()).hexdigest()
        if self._space.info.engine == 'sqlite':
            path = Path(self._space.physical_name)
            sidecar = path.parent / (path.name + '.guard-' + key[:32] + '.sqlite3')
            connection = sqlite3.connect(sidecar, timeout=0 if nowait else 30, isolation_level=None)
            try:
                try:
                    connection.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        raise DatabaseError('DATABASE_GUARD_BUSY') from None
                    raise DatabaseError('DATABASE_BACKEND_UNAVAILABLE') from None
                yield
            finally:
                connection.close()
            return
        with self._space.runtime_engine.connect() as connection:
            postgres = connection.dialect.name == 'postgresql'
            token = int(key[:15], 16) if postgres else 'storage_guard_' + key[:48]
            if postgres:
                statement = 'SELECT pg_try_advisory_lock(:key)' if nowait else 'SELECT pg_advisory_lock(:key)'
            else:
                statement = 'SELECT GET_LOCK(:key,:timeout)'
            acquired = False
            try:
                if postgres and not nowait:
                    connection.execute(text("SELECT set_config('statement_timeout','30000',true)"))
                result = connection.execute(text(statement), {'key': token, 'timeout': 0 if nowait else 30}).scalar()
                acquired = postgres and not nowait or result is True or result == 1
                if not acquired:
                    raise DatabaseError('DATABASE_GUARD_BUSY')
                yield
            except DBAPIError:
                raise DatabaseError('DATABASE_BACKEND_UNAVAILABLE') from None
            finally:
                if acquired and not connection.invalidated:
                    release = 'SELECT pg_advisory_unlock(:key)' if postgres else 'SELECT RELEASE_LOCK(:key)'
                    connection.execute(text(release), {'key': token})

    @contextmanager
    def transaction(self, *, write=True, scope=None, limits=None, operation_id=None):
        if type(write) is not bool:
            raise ValueError('INVALID_DATABASE_TRANSACTION_MODE')
        self.seal()
        selected = {} if scope is None else dict(scope)
        if any(not isinstance(key, str) or not IDENTIFIER.fullmatch(key)
               or value is not None and not isinstance(value, str) for key, value in selected.items()):
            raise ValueError('INVALID_STRUCTURED_SCOPE')
        try:
            isolation = 'REPEATABLE READ' if not write and self._space.info.engine != 'sqlite' else None
            with self._space.transaction(write=write, limits=limits, operation_id=operation_id,
                                         isolation=isolation) as raw:
                if write:
                    locked = raw._execute(select(_WRITE_LOCK.c.lock_id).where(_WRITE_LOCK.c.lock_id == 1).with_for_update())
                    if locked.scalar() != 1:
                        raise DatabaseError('STORAGE_STRUCTURED_LOCK_INVALID')
                if raw._connection.dialect.name == 'postgresql':
                    fields = set().union(*(set(fields) for fields in self._scopes.values()))
                    for key in sorted(fields):
                        raw.execute(SqlStatement("SELECT set_config(:name,:value,true)",
                            {'name': 'storage_scope.' + key, 'value': selected.get(key) or ''}))
                yield StructuredTransaction(raw, self._package, self._metadata, selected, self._scopes, write=write)
        except DBAPIError as error:
            if error.connection_invalidated or error.__class__.__name__ in {'OperationalError', 'InterfaceError'}:
                raise DatabaseError('DATABASE_BACKEND_UNAVAILABLE') from None
            raise


class StructuredTransaction:
    def __init__(self, transaction, package, metadata, scope, scopes, *, write):
        self._transaction, self._package, self._scope = transaction, package, scope
        self._scopes = scopes
        self._metadata = metadata
        self._write = write

    def _run(self, statement, params=None):
        if isinstance(statement, Update):
            ordered = _ordered_update(statement)
            if self._transaction._connection.dialect.name == 'mysql':
                statement = ordered
        replacements = {table: self._metadata.tables[table.name] for table in self._package.metadata.tables.values()}
        for table in self._package.metadata.tables.values():
            replacements.update({column: self._metadata.tables[table.name].c[column.name] for column in table.columns})

        def replace(element):
            if isinstance(element, BindParameter):
                adapted = _storage_type(element.type)
                if adapted is not element.type:
                    result = element._clone()
                    result.type = adapted
                    return result
            if isinstance(element, (Clock, After)):
                result = element._clone()
                result.type = _UtcDateTime()
                return result
            return replacements.get(element)

        adapted = visitors.replacement_traverse(statement, {}, replace)
        result = self._transaction._execute(adapted, params)
        columns = tuple(statement.selected_columns) if isinstance(statement, Select) else tuple(statement._returning)
        if not columns:
            return result
        rows = []
        for row in result.rows:
            rows.append({name: value.decode('utf-8') if isinstance(column.type, ExactString)
                         and isinstance(value, bytes) else value
                         for (name, value), column in zip(row.items(), columns, strict=True)})
        return QueryResult(rows, result.rowcount)

    def _validate(self, statement, parameters):
        for element in visitors.iterate(statement):
            if not isinstance(statement, Select) and isinstance(element, Select):
                raise DatabaseError('STORAGE_STRUCTURED_MUTATION_SUBQUERY_UNSUPPORTED')
            if isinstance(element, TableClause):
                if (not isinstance(element, SchemaTable)
                        or self._package.metadata.tables.get(element.key) is not element):
                    raise DatabaseError('STORAGE_STRUCTURED_TABLE_UNDECLARED')
                if not isinstance(statement, Select) and element is not statement.table:
                    raise DatabaseError('STORAGE_STRUCTURED_MUTATION_JOIN_UNSUPPORTED')
            elif isinstance(element, TextClause) or (isinstance(element, ColumnClause)
                    and (element.is_literal and element.name != '*'
                         or not element.is_literal and element.table is None)):
                raise DatabaseError('STORAGE_STRUCTURED_EXPRESSION_REQUIRED')
            elif (not isinstance(statement, Select) and isinstance(element, ColumnClause)
                  and element.table is not statement.table):
                raise DatabaseError('STORAGE_STRUCTURED_MUTATION_JOIN_UNSUPPORTED')
            elif isinstance(element, FunctionElement):
                if isinstance(element, (Clock, After)):
                    if isinstance(element, After):
                        for argument in element.clauses:
                            value = parameters.get(argument.key, argument.value) if isinstance(argument, BindParameter) else None
                            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                                raise ValueError('STORAGE_INVALID_DURATION')
                elif element.name.lower() not in _SAFE_FUNCTIONS:
                    raise DatabaseError('STORAGE_STRUCTURED_FUNCTION_UNSUPPORTED')
                elif element.name.lower() in {'sum', 'min', 'max', 'avg'}:
                    arguments = tuple(element.clauses)
                    if len(arguments) != 1 or not isinstance(arguments[0].type, (Integer, Float, Numeric)):
                        raise DatabaseError('STORAGE_STRUCTURED_FUNCTION_UNSUPPORTED')

    def _filters(self, table):
        result = []
        for name in self._scopes[table.name]:
            value = self._scope.get(name)
            result.append(table.c[name] == value if value is not None else literal(False))
        return tuple(result)

    def _select(self, statement):
        replacements = {table: select(table).where(*self._filters(table)).subquery(table.name + '_scope')
                        for table in self._package.metadata.tables.values() if self._scopes[table.name]}
        return visitors.replacement_traverse(statement, {}, lambda element: replacements.get(element))

    def _scope_values(self, table, values, parameters):
        for name in self._scopes[table.name]:
            if name not in values:
                continue
            value = values[name]
            if isinstance(value, BindParameter):
                value = parameters.get(value.key, value.value)
            if not isinstance(value, str) or value != self._scope.get(name):
                raise DatabaseError('STORAGE_SCOPE_REPLACEMENT_REJECTED')

    def execute(self, statement, params=None):
        values = {} if params is None else dict(params)
        if not isinstance(statement, (Select, Update, Delete)):
            raise DatabaseError('STORAGE_STRUCTURED_EXPRESSION_REQUIRED')
        self._validate(statement, values)
        if isinstance(statement, Select):
            if not self._write and statement._for_update_arg is not None:
                raise DatabaseError('STORAGE_WRITE_TRANSACTION_REQUIRED')
            return self._run(self._select(statement), values)
        if not self._write:
            raise DatabaseError('STORAGE_WRITE_TRANSACTION_REQUIRED')
        table = statement.table
        if table.name in self._package.appendonly:
            raise DatabaseError('DATABASE_APPENDONLY')
        if isinstance(statement, Update):
            updates = {key.name if hasattr(key, 'name') else key: value for key, value in (statement._values or {}).items()}
            self._scope_values(table, updates, values)
            if any(column.name in updates for column in table.primary_key):
                raise DatabaseError('STORAGE_PRIMARY_KEY_UPDATE_REJECTED')
        statement = statement.where(*self._filters(table))
        returning = tuple(statement._returning)
        if not returning:
            return self._run(statement, values)
        if any(column.table is not table for column in returning):
            raise DatabaseError('STORAGE_STRUCTURED_RETURNING_UNSUPPORTED')
        dialect = self._transaction._connection.dialect
        native = dialect.update_returning if isinstance(statement, Update) else dialect.delete_returning
        if native:
            result = self._run(statement, values)
            return QueryResult(result.rows, len(result.rows))
        before = select(*table.primary_key, *returning).where(*statement._where_criteria).with_for_update()
        captured = self._run(before, values)
        if not captured.rows:
            return QueryResult((), 0)
        mutation = statement._generate()
        mutation._returning = ()
        primary = tuple(table.primary_key)
        changed = []
        for row in captured.rows:
            identity = and_(*(column == row[column.name] for column in primary))
            result = self._run(mutation.where(identity), values)
            if result.rowcount:
                changed.append(row)
        if isinstance(statement, Delete):
            names = tuple(column.key for column in returning)
            return QueryResult(({name: row[name] for name in names} for row in changed), len(changed))
        if not changed:
            return QueryResult((), 0)
        identities = [and_(*(column == row[column.name] for column in primary)) for row in changed]
        result = self._run(select(*returning).where(or_(*identities)), values)
        return QueryResult(result.rows, len(changed))

    def insert(self, table, values, *, conflict_keys=(), updates=None, returning=()):
        if not self._write:
            raise DatabaseError('STORAGE_WRITE_TRANSACTION_REQUIRED')
        if not isinstance(table, SchemaTable) or self._package.metadata.tables.get(table.key) is not table:
            raise DatabaseError('STORAGE_STRUCTURED_TABLE_UNDECLARED')
        if not isinstance(values, Mapping) or updates is not None and not isinstance(updates, Mapping):
            raise ValueError('INVALID_STRUCTURED_VALUES')
        if updates is not None and table.name in self._package.appendonly:
            raise DatabaseError('DATABASE_APPENDONLY')
        incoming = dict(values)
        self._scope_values(table, incoming, incoming)
        for name in self._scopes[table.name]:
            if self._scope.get(name) is None or incoming.get(name) != self._scope[name]:
                raise DatabaseError('STORAGE_SCOPE_REPLACEMENT_REJECTED')
        keys = tuple(conflict_keys)
        if any(name not in table.c or name not in incoming for name in keys):
            raise ValueError('INVALID_STRUCTURED_CONFLICT_KEYS')
        constraints = {frozenset(column.name for column in table.primary_key)}
        constraints.update(frozenset(column.name for column in constraint.columns)
                           for constraint in table.constraints if isinstance(constraint, UniqueConstraint))
        constraints.update(frozenset(column.name for column in index.columns) for index in table.indexes if index.unique)
        if keys and frozenset(keys) not in constraints or updates is not None and not keys:
            raise ValueError('INVALID_STRUCTURED_CONFLICT_KEYS')
        existing = None
        condition = and_(*(table.c[name] == incoming[name] for name in keys)) if keys else None
        if keys:
            lookup = select(table).where(condition)
            if table.name not in self._package.appendonly:
                lookup = lookup.with_for_update()
            existing = self.execute(lookup).first()
        if existing is not None:
            if updates is None:
                return QueryResult((), 0)
            statement = table.update().where(condition).values(dict(updates))
            if returning:
                statement = statement.returning(*returning)
            return self.execute(statement)
        statement = table.insert().values(incoming)
        self._validate(statement, incoming)
        result = self._run(statement)
        if not returning:
            return result
        if not all(column.name in incoming for column in table.primary_key):
            raise ValueError('STORAGE_PRIMARY_KEY_REQUIRED')
        condition = and_(*(column == incoming[column.name] for column in table.primary_key))
        row = self.execute(select(*returning).where(condition))
        return QueryResult(row.rows, result.rowcount)
