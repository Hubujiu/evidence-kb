from sqlalchemy import Column, Float, ForeignKey, Integer, MetaData, String, Table, Text

from ..storage_sdk.relational import ExactString, StructuredSchema


metadata = MetaData()
sessions = Table('sessions', metadata,
    Column('id', ExactString(64), primary_key=True),
    Column('tenant', ExactString(256), nullable=False),
    Column('principal', ExactString(256), nullable=False),
    Column('scope', Text, nullable=False),
    Column('revision', Integer, nullable=False),
    Column('state', Text, nullable=False),
    Column('created', String(40), nullable=False),
    Column('updated', String(40), nullable=False))
events = Table('events', metadata,
    Column('session', ExactString(64), ForeignKey('sessions.id'), primary_key=True),
    Column('seq', Integer, primary_key=True),
    Column('kind', Text, nullable=False),
    Column('payload', Text, nullable=False),
    Column('group_id', Text, nullable=False),
    Column('agent', Text, nullable=False),
    Column('created', String(40), nullable=False))
requests = Table('requests', metadata,
    Column('session', ExactString(64), ForeignKey('sessions.id'), primary_key=True),
    Column('request_id', ExactString(256), primary_key=True),
    Column('fingerprint', ExactString(256), nullable=False),
    Column('token', ExactString(64), nullable=False),
    Column('status', String(20), nullable=False),
    Column('expires', Float, nullable=False),
    Column('response', Text))
artifacts = Table('artifacts', metadata,
    Column('session', ExactString(64), ForeignKey('sessions.id'), primary_key=True),
    Column('id', ExactString(64), primary_key=True),
    Column('body', Text, nullable=False),
    Column('created', String(40), nullable=False))


def session_schema(owner):
    return StructuredSchema(owner, metadata, appendonly=('events', 'artifacts'))
