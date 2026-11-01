from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import json
import sqlite3
from time import monotonic
from uuid import uuid4
import re

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ..database_contracts import CommitUnknown, DatabaseError, QueryLimits, QueryResult, SqlStatement


_RUNTIME_STATEMENT = re.compile(
    r'\A(?:[\s\ufeff;]+|--[^\r\n]*(?:\r\n?|\n|$)|\#[^\r\n]*(?:\r\n?|\n|$)|'
    r'/\*(?![!+]|[mM]!)[\s\S]*?\*/)*([A-Za-z]+)\b')
_RUNTIME_COMMANDS = frozenset({'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'WITH', 'VALUES', 'REPLACE', 'MERGE'})


class Transaction:
    def __init__(self, connection, engine, limits, *, write=True):
        self._connection, self._engine, self._limits = connection, engine, limits
        self._expires = monotonic() + limits.max_seconds
        self._statements, self._rows, self._bytes = 0, 0, 0
        self._active, self._write = True, write

    def execute(self, sql, params=None):
        if not self._active:
            raise DatabaseError('DATABASE_TRANSACTION_CLOSED')
        statement = sql if isinstance(sql, SqlStatement) else SqlStatement(sql, {} if params is None else params)
        command = _RUNTIME_STATEMENT.match(statement.sql)
        if (command is None or command.group(1).upper() not in _RUNTIME_COMMANDS or
                self._engine == 'mysql' and re.search(r'/\*(?:!|[mM]!)', statement.sql)):
            raise DatabaseError('DATABASE_RUNTIME_STATEMENT_REQUIRED')
        if self._engine == 'sqlite':
            if command.group(1).upper() == 'REPLACE':
                raise DatabaseError('DATABASE_RUNTIME_STATEMENT_REQUIRED')
            if command.group(1).upper() == 'INSERT':
                modifier = _RUNTIME_STATEMENT.match(statement.sql[command.end():])
                if modifier is not None and modifier.group(1).upper() == 'OR':
                    policy = _RUNTIME_STATEMENT.match(statement.sql[command.end() + modifier.end():])
                    if policy is not None and policy.group(1).upper() in {'REPLACE', 'ROLLBACK'}:
                        raise DatabaseError('DATABASE_RUNTIME_STATEMENT_REQUIRED')
        return self._execute(text(statement.sql), dict(statement.params), expected_rows=statement.expected_rows)

    def _execute(self, statement, params=None, *, expected_rows=None):
        if not self._active:
            raise DatabaseError('DATABASE_TRANSACTION_CLOSED')
        self._statements += 1
        if self._statements > self._limits.max_statements or monotonic() > self._expires:
            raise DatabaseError('DATABASE_QUERY_LIMIT')
        result = self._connection.execute(statement, {} if params is None else dict(params))
        rows = []
        if result.returns_rows:
            for row in result.mappings():
                value = dict(row)
                self._rows += 1
                self._bytes += len(json.dumps(value, default=_size_value, ensure_ascii=True).encode())
                if (self._rows > self._limits.max_rows or self._bytes > self._limits.max_bytes or
                        monotonic() > self._expires):
                    result.close()
                    raise DatabaseError('DATABASE_RESULT_LIMIT')
                rows.append(value)
        count = result.rowcount if result.rowcount >= 0 else len(rows)
        result.close()
        if monotonic() > self._expires:
            raise DatabaseError('DATABASE_QUERY_TIMEOUT')
        if expected_rows is not None and count != expected_rows:
            raise DatabaseError('DATABASE_ROWCOUNT_CONFLICT')
        return QueryResult(rows, count)

    query = execute


def _size_value(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    raise DatabaseError('UNSUPPORTED_DATABASE_VALUE')


@contextmanager
def transaction(space, *, write=True, limits=None, migration=False, operation_id=None, isolation=None):
    limits = QueryLimits() if limits is None else limits
    engine = space.migration_engine if migration else space.runtime_engine
    with engine.connect() as connection:
        if isolation is not None:
            connection = connection.execution_options(isolation_level=isolation)
        controls = {'finishing': False}
        if space.info.engine == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE' if write else 'BEGIN')
            raw = connection.connection.driver_connection
            expires = monotonic() + limits.max_seconds
            raw.set_progress_handler(lambda: int(monotonic() > expires), 1000)
            if not migration:
                raw.set_authorizer(_sqlite_authorizer(write, space.runtime_readonly, space.runtime_appendonly,
                                                     space.runtime_readonly_prefixes, controls))
        else:
            connection.begin()
            space.initialize_transaction(connection, write=write, limits=limits)
        tx = Transaction(connection, space.info.engine, limits, write=write)
        try:
            yield tx
        except BaseException:
            controls['finishing'] = True
            connection.rollback()
            raise
        else:
            controls['finishing'] = True
            try:
                connection.commit()
            except DBAPIError as error:
                if write and error.connection_invalidated:
                    raise CommitUnknown(operation_id or uuid4().hex) from None
                raise DatabaseError('DATABASE_COMMIT_FAILED') from None
        finally:
            tx._active = False
            if space.info.engine == 'sqlite' and not connection.invalidated:
                raw.set_authorizer(None)
                raw.set_progress_handler(None, 0)


def _sqlite_authorizer(write, readonly, appendonly, readonly_prefixes, controls):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               sqlite3.SQLITE_RECURSIVE}
    if write:
        allowed.update((sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE))

    def authorize(action, first, second, database, source):
        if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}:
            return sqlite3.SQLITE_OK if controls['finishing'] else sqlite3.SQLITE_DENY
        if action not in allowed or action == sqlite3.SQLITE_FUNCTION and second == 'load_extension':
            return sqlite3.SQLITE_DENY
        if first is not None and first.startswith('_database_'):
            return sqlite3.SQLITE_DENY
        if (action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and first is not None
                and (first in readonly or first.startswith(readonly_prefixes))):
            return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and first in appendonly:
            return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and first in {
                'sqlite_master', 'sqlite_schema', 'sqlite_temp_master', 'sqlite_temp_schema'}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


class DatabaseSpace:
    def __init__(self, provider, key, runtime_engine, migration_engine, physical_name):
        self.provider, self.key, self.info = provider, key, provider.info
        self.runtime_engine, self.migration_engine, self.physical_name = runtime_engine, migration_engine, physical_name
        self.identity = None
        self.runtime_readonly = ()
        self.runtime_appendonly = ()
        self.runtime_readonly_prefixes = ()

    def initialize_transaction(self, connection, *, write, limits):
        if self.info.engine == 'postgresql':
            if not write:
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            connection.execute(text("SELECT set_config('search_path', :path, true)"),
                               {'path': self.physical_name + ', pg_catalog'})
            connection.execute(text("SELECT set_config('statement_timeout', :timeout, true)"),
                               {'timeout': str(max(1, int(limits.max_seconds * 1000)))})
        elif self.info.engine == 'mysql':
            if not write:
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            connection.exec_driver_sql('SET SESSION MAX_EXECUTION_TIME=' +
                                      str(max(1, int(limits.max_seconds * 1000))))

    def transaction(self, **options):
        return transaction(self, **options)

    def grant_runtime(self, package):
        self.provider.grant_runtime(self, package)

    def close(self):
        self.runtime_engine.dispose()
        self.migration_engine.dispose()
