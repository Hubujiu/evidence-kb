from threading import RLock
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ..database_contracts import DatabaseError, DatabaseInfo
from .base import DatabaseSpace


class MySQLProvider:
    info = DatabaseInfo('mysql', frozenset({'transactions', 'named_parameters', 'bounded_results',
                                         'row_locks', 'mysql'}))

    def __init__(self, url, migration_url=None, *, provision=True, database=None):
        self._url = _url(url)
        self._migration_url = _url(migration_url) if migration_url else self._url
        self._provision, self._database = provision, database
        self._spaces, self._users, self._lock = {}, [], RLock()

    def open_space(self, key):
        with self._lock:
            if key in self._spaces:
                return self._spaces[key]
            database = self._database or 'kb_' + key.digest[:24]
            admin = create_engine(self._migration_url, connect_args=_connect_args())
            runtime_url = self._url.set(database=database)
            if self._provision:
                user, password = 'dsrun_' + uuid4().hex[:24], uuid4().hex + uuid4().hex
                with admin.begin() as conn:
                    quote = conn.dialect.identifier_preparer.quote
                    conn.exec_driver_sql('CREATE DATABASE IF NOT EXISTS ' + quote(database) +
                                         ' CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
                    conn.execute(text("CREATE USER :user@'%' IDENTIFIED BY :password"),
                                 {'user': user, 'password': password})
                self._users.append((admin, user))
                runtime_url = runtime_url.set(username=user, password=password)
            else:
                admin.dispose()
            migration = create_engine(self._migration_url.set(database=database), connect_args=_connect_args())
            runtime = create_engine(runtime_url, pool_size=4, max_overflow=0, connect_args=_connect_args())
            space = DatabaseSpace(self, key, runtime, migration, database)
            if not self._provision:
                try:
                    self.grant_runtime(space, None)
                except BaseException:
                    space.close()
                    raise
            self._spaces[key] = space
            return space

    def grant_runtime(self, space, package):
        with space.migration_engine.connect() as conn:
            rows = conn.execute(text('SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES '
                                     "WHERE TABLE_SCHEMA=:database AND TABLE_TYPE='BASE TABLE'"),
                                {'database': space.physical_name})
            if any(str(row[1]).lower() != 'innodb' for row in rows):
                raise DatabaseError('DATABASE_NONTRANSACTIONAL_TABLE')
        if not self._provision:
            with space.runtime_engine.connect() as conn:
                grants = [str(row[0]).upper() for row in conn.exec_driver_sql('SHOW GRANTS')]
                if any('ALL PRIVILEGES' in grant or 'CREATE' in grant or 'SUPER' in grant for grant in grants):
                    raise DatabaseError('UNRESTRICTED_DATABASE_RUNTIME_ROLE')
                grantee = conn.exec_driver_sql("SELECT CONCAT(QUOTE(SUBSTRING_INDEX(CURRENT_USER(),'@',1)),"
                                              "'@',QUOTE(SUBSTRING_INDEX(CURRENT_USER(),'@',-1)))").scalar()
                metadata = conn.execute(text('SELECT TABLE_NAME FROM information_schema.TABLES '
                    'WHERE TABLE_SCHEMA=:database'), {'database': space.physical_name})
                for table in metadata:
                    if table[0].startswith('_database_'):
                        privileges = conn.execute(text('SELECT PRIVILEGE_TYPE FROM information_schema.USER_PRIVILEGES '
                            'WHERE GRANTEE=:grantee UNION ALL '
                            'SELECT PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES '
                            'WHERE GRANTEE=:grantee AND TABLE_SCHEMA=:database UNION ALL '
                            'SELECT PRIVILEGE_TYPE FROM information_schema.TABLE_PRIVILEGES '
                            'WHERE GRANTEE=:grantee AND TABLE_SCHEMA=:database AND TABLE_NAME=:table UNION ALL '
                            'SELECT PRIVILEGE_TYPE FROM information_schema.COLUMN_PRIVILEGES '
                            'WHERE GRANTEE=:grantee AND TABLE_SCHEMA=:database AND TABLE_NAME=:table'),
                            {'grantee': grantee, 'database': space.physical_name, 'table': table[0]})
                        if any(row[0] in {'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'TRIGGER', 'REFERENCES'}
                               for row in privileges):
                            raise DatabaseError('DATABASE_RUNTIME_METADATA_WRITE_PRIVILEGE')
            return
        with space.migration_engine.begin() as conn:
            quote = conn.dialect.identifier_preparer.quote
            conn.execute(text("REVOKE ALL PRIVILEGES, GRANT OPTION FROM '" +
                              space.runtime_engine.url.username + "'@'%'"))
            tables = tuple(row[0] for row in conn.execute(
                text('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=:database'),
                {'database': space.physical_name}))
            for table in tables:
                if not table.startswith('_database_'):
                    conn.execute(text('GRANT SELECT, INSERT, UPDATE, DELETE ON ' +
                                      quote(space.physical_name) + '.' + quote(table) + " TO '" +
                                      space.runtime_engine.url.username + "'@'%'"))
            readonly = set(package.runtime_readonly) | {
                table for table in tables if table.startswith(package.runtime_readonly_prefixes)}
            for table in sorted(readonly):
                conn.execute(text('REVOKE INSERT, UPDATE, DELETE ON ' + quote(space.physical_name) + '.' +
                                  quote(table) + " FROM '" + space.runtime_engine.url.username + "'@'%'"))
            for table in package.runtime_appendonly:
                conn.execute(text('REVOKE UPDATE, DELETE ON ' + quote(space.physical_name) + '.' +
                                  quote(table) + " FROM '" + space.runtime_engine.url.username + "'@'%'"))

    def close(self):
        with self._lock:
            for space in self._spaces.values():
                space.close()
            for engine, user in reversed(self._users):
                with engine.begin() as conn:
                    conn.execute(text("DROP USER IF EXISTS :user@'%'"), {'user': user})
                engine.dispose()
            self._spaces.clear()
            self._users.clear()


def _url(value):
    url = make_url(value)
    if url.get_backend_name() != 'mysql':
        raise ValueError('INVALID_MYSQL_DATABASE_URL')
    return url.set(drivername='mysql+pymysql')


def _connect_args():
    return {'connect_timeout': 10, 'read_timeout': 30, 'write_timeout': 30, 'charset': 'utf8mb4'}
