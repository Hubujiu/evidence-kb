from contextlib import closing
from pathlib import Path
import sqlite3

from .database_contracts import SpaceKey
from .database_providers.sqlite import SQLiteProvider
from .database_service import DatabaseService


def local_database(path, *, owner):
    path = Path(path).resolve()
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and '_database_identity' not in tables:
            raise ValueError('LEGACY_PERSISTENCE_REQUIRES_EXPLICIT_IMPORT')
    key = SpaceKey('local-persistence', owner, 'primary')
    service = DatabaseService()
    service.register('local', SQLiteProvider(path.parent, files={key: path}))
    try:
        bound = service.bind(service.grant('local', key, owner))
    except BaseException:
        service.close()
        raise
    return bound, service


def sealed_local_ledger(path):
    target = Path(str(Path(path).resolve()) + '.producer.sqlite3')
    if not target.exists():
        return False
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute('SELECT successor FROM producer WHERE id=1').fetchone() is not None
