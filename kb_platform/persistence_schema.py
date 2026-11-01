from sqlalchemy import Column, Integer, MetaData, String, Table, Text

from .storage_sdk.relational import ExactString, StructuredSchema


cache_metadata = MetaData()
entries = Table('entries', cache_metadata,
    Column('key', ExactString(64), primary_key=True),
    Column('hash', String(64), nullable=False),
    Column('object_ref', Text))

budget_metadata = MetaData()
budget_config = Table('config', budget_metadata,
    Column('id', Integer, primary_key=True),
    Column('caps', Text, nullable=False))
budget_calls = Table('calls', budget_metadata,
    Column('id', ExactString(200), primary_key=True),
    Column('status', String(16), nullable=False),
    Column('reserved', Text, nullable=False),
    Column('usage', Text),
    Column('category', Text, nullable=False),
    Column('diagnostic', Text))
budget_guard = Table('producer_guard', budget_metadata,
    Column('id', Integer, primary_key=True),
    Column('successor', Text))
ledger_imports = Table('ledger_imports', budget_metadata,
    Column('source_sha256', String(64), primary_key=True),
    Column('snapshot_sha256', String(64), nullable=False),
    Column('source_path', Text, nullable=False),
    Column('record_count', Integer, nullable=False))


def cache_schema(owner):
    return StructuredSchema(owner, cache_metadata)


def budget_schema(owner):
    return StructuredSchema(owner, budget_metadata)
