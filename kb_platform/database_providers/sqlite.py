from pathlib import Path
from hashlib import sha256
from functools import partial
from threading import RLock
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool, QueuePool

from ..database_contracts import DatabaseInfo
from .base import DatabaseSpace


class SQLiteProvider:
    info = DatabaseInfo('sqlite')

    def __init__(self, root, *, files=None):
        if files is None and sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError('SQLITE_VERSION_REQUIRES_3_51_3')
        self.root = Path(root).resolve()
        self._files = None if files is None else {key: Path(path).resolve() for key, path in files.items()}
        if self._files is not None and len(set(self._files.values())) != len(self._files):
            raise ValueError('DUPLICATE_SQLITE_SPACE_FILE')
        self._spaces, self._lock = {}, RLock()

    def open_space(self, key):
        with self._lock:
            if key in self._spaces:
                return self._spaces[key]
            if self._files is None:
                path = self.root / key.digest / 'space.sqlite3'
            else:
                if key not in self._files:
                    raise ValueError('SQLITE_SPACE_FILE_NOT_GRANTED')
                path = self._files[key]
            path.parent.mkdir(parents=True, exist_ok=True)
            pooling = ({'poolclass': QueuePool, 'pool_size': 1, 'max_overflow': -1}
                       if self._files is None else {'poolclass': NullPool})
            engines = [create_engine('sqlite:///' + path.as_posix(), **pooling,
                                     connect_args={'timeout': 30, 'check_same_thread': False}) for _ in range(2)]
            for engine in engines:
                event.listen(engine, 'connect', partial(_initialize, wal=self._files is None))
            space = DatabaseSpace(self, key, engines[0], engines[1], str(path))
            self._spaces[key] = space
            return space

    def grant_runtime(self, space, package):
        with space.migration_engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                quote = connection.dialect.identifier_preparer.quote
                for table in package.runtime_appendonly:
                    prefix = '_database_appendonly_' + sha256(table.encode('utf-8')).hexdigest()[:24]
                    for operation in ('UPDATE', 'DELETE'):
                        name = prefix + '_' + operation.lower()
                        connection.exec_driver_sql('CREATE TRIGGER IF NOT EXISTS ' + quote(name) +
                            ' BEFORE ' + operation + ' ON ' + quote(table) +
                            " BEGIN SELECT RAISE(ABORT,'DATABASE_APPENDONLY'); END")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        space.runtime_readonly = package.runtime_readonly
        space.runtime_appendonly = package.runtime_appendonly
        space.runtime_readonly_prefixes = package.runtime_readonly_prefixes

    def close(self):
        with self._lock:
            for space in self._spaces.values():
                space.close()
            self._spaces.clear()


def _initialize(connection, record, *, wal):
    mode = 'wal' if wal else 'delete'
    if connection.execute('PRAGMA journal_mode=' + mode).fetchone()[0] != mode:
        raise RuntimeError('SQLITE_WAL_UNAVAILABLE')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA recursive_triggers=ON')
    connection.execute('PRAGMA busy_timeout=30000')
