from contextlib import contextmanager
from threading import RLock
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ..database_contracts import DatabaseError, DatabaseInfo
from .base import DatabaseSpace


class PostgresProvider:
    info = DatabaseInfo('postgresql', frozenset({'transactions', 'named_parameters', 'bounded_results',
                                               'transactional_ddl', 'row_locks', 'postgresql'}))

    def __init__(self, url, migration_url=None, *, provision=True, schema=None):
        self._url = _url(url)
        self._migration_url = _url(migration_url) if migration_url else self._url
        self._provision, self._schema = provision, schema
        self._spaces, self._lock, self._roles = {}, RLock(), []

    def open_space(self, key):
        with self._lock:
            if key in self._spaces:
                return self._spaces[key]
            schema = self._schema or 'kb_' + key.digest[:24]
            migration = create_engine(self._migration_url, pool_size=2, max_overflow=0,
                                      connect_args={'connect_timeout': 10})
            runtime_url = self._url
            if self._provision:
                role, password = 'dsrun_' + uuid4().hex[:24], uuid4().hex + uuid4().hex
                with migration.begin() as conn:
                    quote = conn.dialect.identifier_preparer.quote
                    conn.exec_driver_sql('CREATE SCHEMA IF NOT EXISTS ' + quote(schema))
                    conn.exec_driver_sql('REVOKE ALL ON SCHEMA ' + quote(schema) + ' FROM PUBLIC')
                    conn.exec_driver_sql('CREATE ROLE ' + quote(role) +
                                         " LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '" +
                                         password + "'")
                    conn.exec_driver_sql('GRANT USAGE ON SCHEMA ' + quote(schema) + ' TO ' + quote(role))
                self._roles.append((migration, role))
                runtime_url = self._url.set(username=role, password=password)
            runtime = create_engine(runtime_url, pool_size=4, max_overflow=0,
                                    connect_args={'connect_timeout': 10, 'prepare_threshold': 0})
            space = DatabaseSpace(self, key, runtime, migration, schema)
            if not self._provision:
                try:
                    self.grant_runtime(space, None)
                except BaseException:
                    space.close()
                    raise
            self._spaces[key] = space
            return space

    def grant_runtime(self, space, package):
        if not self._provision:
            with space.runtime_engine.connect() as conn:
                row = conn.execute(text('SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls '
                                        'FROM pg_roles WHERE rolname=current_user')).one()
                if any(row):
                    raise DatabaseError('UNRESTRICTED_DATABASE_RUNTIME_ROLE')
                can_create = conn.execute(text('SELECT has_schema_privilege(current_user,:schema,:privilege)'),
                                          {'schema': space.physical_name, 'privilege': 'CREATE'}).scalar()
                if can_create:
                    raise DatabaseError('DATABASE_RUNTIME_DDL_PRIVILEGE')
                reserved = conn.execute(text('SELECT c.oid,c.relname FROM pg_class c '
                    'JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema '
                    "AND c.relkind IN ('r','p')"), {'schema': space.physical_name})
                for table in reserved:
                    if table.relname.startswith('_database_'):
                        writable = conn.execute(text('SELECT has_table_privilege(current_user,:table,'
                            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                            "has_any_column_privilege(current_user,:table,'INSERT,UPDATE,REFERENCES')"),
                            {'table': table.oid}).scalar()
                        if writable:
                            raise DatabaseError('DATABASE_RUNTIME_METADATA_WRITE_PRIVILEGE')
            return
        with space.migration_engine.begin() as conn:
            role = space.runtime_engine.url.username
            quote = conn.dialect.identifier_preparer.quote
            tables = tuple(row.tablename for row in conn.execute(
                text('SELECT tablename FROM pg_tables WHERE schemaname=:schema'), {'schema': space.physical_name}))
            for table in tables:
                if table.startswith('_database_'):
                    conn.exec_driver_sql('REVOKE ALL ON ' + quote(space.physical_name) + '.' + quote(table) +
                                         ' FROM ' + quote(role) + ', PUBLIC')
                else:
                    conn.exec_driver_sql('GRANT SELECT, INSERT, UPDATE, DELETE ON ' +
                                         quote(space.physical_name) + '.' + quote(table) + ' TO ' + quote(role))
            conn.exec_driver_sql('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ' +
                                 quote(space.physical_name) + ' TO ' + quote(role))
            readonly = set(package.runtime_readonly) | {
                table for table in tables if table.startswith(package.runtime_readonly_prefixes)}
            for table in sorted(readonly):
                conn.exec_driver_sql('REVOKE INSERT, UPDATE, DELETE ON ' + quote(space.physical_name) + '.' +
                                     quote(table) + ' FROM ' + quote(role))
            for function in package.runtime_functions:
                conn.exec_driver_sql('GRANT EXECUTE ON FUNCTION ' + quote(space.physical_name) + '.' +
                                     function + ' TO ' + quote(role))
            for table in package.runtime_appendonly:
                conn.exec_driver_sql('REVOKE UPDATE, DELETE ON ' + quote(space.physical_name) + '.' +
                                     quote(table) + ' FROM ' + quote(role))

    def close(self):
        with self._lock:
            for space in self._spaces.values():
                space.runtime_engine.dispose()
            for engine, role in reversed(self._roles):
                with engine.begin() as conn:
                    quote = conn.dialect.identifier_preparer.quote
                    conn.exec_driver_sql('DROP OWNED BY ' + quote(role))
                    conn.exec_driver_sql('DROP ROLE ' + quote(role))
            for space in self._spaces.values():
                space.migration_engine.dispose()
            self._spaces.clear()
            self._roles.clear()


def _url(value):
    url = make_url(value)
    if url.get_backend_name() not in {'postgresql', 'postgres'}:
        raise ValueError('INVALID_POSTGRES_DATABASE_URL')
    return url.set(drivername='postgresql+psycopg')


@contextmanager
def pg_transaction(bound, tenant_id=None, kb_id=None, *, write=True):
    from psycopg.rows import dict_row

    if bound.info.engine != 'postgresql':
        raise DatabaseError('POSTGRES_DATABASE_REQUIRED')
    with bound.transaction(write=write) as tx:
        quote = tx._connection.dialect.identifier_preparer.quote
        tx.execute("SELECT set_config('search_path', :path, true)",
                   {'path': quote(bound.physical_name) + ', pg_catalog, public'})
        tx.execute("SELECT set_config('app.tenant_id', :tenant, true)", {'tenant': tenant_id or ''})
        tx.execute("SELECT set_config('app.kb_id', :kb, true)", {'kb': kb_id or ''})
        raw = tx._connection.connection.driver_connection
        previous = raw.row_factory
        raw.row_factory = dict_row
        try:
            yield raw
        finally:
            raw.row_factory = previous
