from contextlib import contextmanager
import sqlite3

from ..database_contracts import DatabaseError


@contextmanager
def sqlite_guard(path, *, nowait=True):
    connection = sqlite3.connect(path, timeout=0 if nowait else 30, isolation_level=None)
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
