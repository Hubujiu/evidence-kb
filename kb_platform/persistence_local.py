from contextlib import closing
from pathlib import Path
import sqlite3

from .database_contracts import SpaceKey
from .database_providers.sqlite import SQLiteProvider
from .storage_adapters.sql import SqlStorageProvider
from .storage_sdk import ExistingDataset


def _open_local(provider, space):
    storage = SqlStorageProvider(provider)
    try:
        identity = storage.initialize('structured', space)
        exported = next(item for item in storage.exports() if item.name == 'structured')
        bound = exported.open_space(space, ExistingDataset(identity.dataset_id))
    except BaseException:
        storage.close()
        raise
    return bound, storage


def local_space(root, space):
    return _open_local(SQLiteProvider(root), space)


def local_database(path, *, owner):
    path = Path(path).resolve()
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and '_database_identity' not in tables:
            raise ValueError('LEGACY_PERSISTENCE_REQUIRES_EXPLICIT_IMPORT')
    key = SpaceKey('local-persistence', owner, 'primary')
    return _open_local(SQLiteProvider(path.parent, files={key: path}), key)


def read_frozen_ledger(path, identities=None):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute('BEGIN')
        rows = [dict(row) for row in database.execute(
            'SELECT id,status,reserved,usage,category,diagnostic FROM calls')]
    if identities is None:
        return rows
    selected = set(identities)
    return [row for row in rows if row['id'] in selected]


def sealed_local_ledger(path):
    target = Path(str(Path(path).resolve()) + '.producer.sqlite3')
    if not target.exists():
        return False
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute('SELECT successor FROM producer WHERE id=1').fetchone() is not None
