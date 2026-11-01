from contextlib import contextmanager
from hashlib import sha256
import json

from sqlalchemy import Column, MetaData, Table, Text, select

from .storage_sdk.relational import ExactString, StructuredSchema


metadata = MetaData()
receipt_logical = Table('receipt_logical', metadata,
    Column('logical_hash', ExactString(64), primary_key=True),
    Column('row_json', Text, nullable=False))
receipt_physical = Table('receipt_physical', metadata,
    Column('physical_id', ExactString(32), primary_key=True),
    Column('row_json', Text, nullable=False))


class ReceiptDatabase:
    def __init__(self, database):
        self.database = database
        owner = database.identity.space.owner_id
        database.prepare(StructuredSchema(owner, metadata))
        database.seal()

    @contextmanager
    def _locked(self, logical_id, *, create=False):
        logical_hash = sha256(logical_id.encode('utf-8')).hexdigest()
        with self.database.transaction(write=True) as db:
            if create:
                db.insert(receipt_logical, {'logical_hash': logical_hash, 'row_json': '{}'},
                          conflict_keys=('logical_hash',))
            row = db.execute(select(receipt_logical.c.row_json).where(
                receipt_logical.c.logical_hash == logical_hash).with_for_update()).fetchone()
            yield db, logical_hash, None if row is None else json.loads(row['row_json'])

    @staticmethod
    def _save(db, logical_hash, row, *, create=False):
        physical, serialized = row['physical_request_id'], json.dumps(row, ensure_ascii=False)
        if create:
            db.insert(receipt_physical, {'physical_id': physical, 'row_json': serialized})
        else:
            db.execute(receipt_physical.update().where(receipt_physical.c.physical_id == physical)
                       .values(row_json=serialized))
        db.execute(receipt_logical.update().where(receipt_logical.c.logical_hash == logical_hash)
                   .values(row_json=serialized))

    def begin(self, logical_id, row, validate):
        with self._locked(logical_id, create=True) as (db, logical_hash, previous):
            if previous:
                validate(previous)
            self._save(db, logical_hash, row, create=True)
        return row['physical_request_id']

    def transition(self, logical_id, physical_id, mutate):
        with self._locked(logical_id) as (db, logical_hash, active):
            row = db.execute(select(receipt_physical.c.row_json).where(
                             receipt_physical.c.physical_id == physical_id)).fetchone()
            value = None if row is None else json.loads(row['row_json'])
            mutate(value, active)
            self._save(db, logical_hash, value)

    def read(self, physical_id):
        with self.database.transaction(write=False) as db:
            row = db.execute(select(receipt_physical.c.row_json).where(
                             receipt_physical.c.physical_id == physical_id)).fetchone()
        return None if row is None else json.loads(row['row_json'])
