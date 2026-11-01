from contextlib import contextmanager
from hashlib import sha256
import json

from .database_contracts import Migration, SchemaPackage


class ReceiptDatabase:
    def __init__(self, database):
        self.database = database
        owner = database.identity.space.owner_id
        database.prepare(SchemaPackage(owner, (Migration(1, (
            'CREATE TABLE receipt_logical (logical_hash VARCHAR(64) PRIMARY KEY, row_json TEXT NOT NULL)',
            'CREATE TABLE receipt_physical (physical_id VARCHAR(32) PRIMARY KEY, row_json TEXT NOT NULL)',
        )),)))
        database.seal()

    @contextmanager
    def _locked(self, logical_id, *, create=False):
        logical_hash = sha256(logical_id.encode('utf-8')).hexdigest()
        with self.database.transaction(write=True) as db:
            if create:
                sql = ('INSERT IGNORE INTO receipt_logical(logical_hash,row_json) VALUES(:hash,:row)'
                       if self.database.info.engine == 'mysql' else
                       'INSERT INTO receipt_logical(logical_hash,row_json) VALUES(:hash,:row) '
                       'ON CONFLICT(logical_hash) DO NOTHING')
                db.execute(sql, {'hash': logical_hash, 'row': '{}'})
            suffix = '' if self.database.info.engine == 'sqlite' else ' FOR UPDATE'
            row = db.execute('SELECT row_json FROM receipt_logical WHERE logical_hash=:hash' + suffix,
                             {'hash': logical_hash}).fetchone()
            yield db, logical_hash, None if row is None else json.loads(row['row_json'])

    @staticmethod
    def _save(db, logical_hash, row, *, create=False):
        values = {'physical': row['physical_request_id'], 'row': json.dumps(row, ensure_ascii=False)}
        if create:
            db.execute('INSERT INTO receipt_physical(physical_id,row_json) VALUES(:physical,:row)', values)
        else:
            db.execute('UPDATE receipt_physical SET row_json=:row WHERE physical_id=:physical', values)
        db.execute('UPDATE receipt_logical SET row_json=:row WHERE logical_hash=:hash',
                   {'row': values['row'], 'hash': logical_hash})

    def begin(self, logical_id, row, validate):
        with self._locked(logical_id, create=True) as (db, logical_hash, previous):
            if previous:
                validate(previous)
            self._save(db, logical_hash, row, create=True)
        return row['physical_request_id']

    def transition(self, logical_id, physical_id, mutate):
        with self._locked(logical_id) as (db, logical_hash, active):
            row = db.execute('SELECT row_json FROM receipt_physical WHERE physical_id=:physical',
                             {'physical': physical_id}).fetchone()
            value = None if row is None else json.loads(row['row_json'])
            mutate(value, active)
            self._save(db, logical_hash, value)

    def read(self, physical_id):
        with self.database.transaction(write=False) as db:
            row = db.execute('SELECT row_json FROM receipt_physical WHERE physical_id=:physical',
                             {'physical': physical_id}).fetchone()
        return None if row is None else json.loads(row['row_json'])
