from sqlalchemy import (
    BigInteger, CheckConstraint, Column, ForeignKeyConstraint, Integer, MetaData,
    PrimaryKeyConstraint, Table, Text, UniqueConstraint,
)

from .storage_sdk.relational import Clock, ExactString, StructuredSchema, TypedJSON, UtcDateTime


METADATA = MetaData()


def _identity(name, *, nullable=False, principal=False):
    return Column(name, ExactString(200 if principal else 128), nullable=nullable)


def _table(name, *columns, primary, scope=('tenant_id',), constraints=()):
    return Table(name, METADATA, *columns, PrimaryKeyConstraint(*primary), *constraints, info={'scope': scope})


TENANTS = _table('tenant_state', _identity('tenant_id', principal=True),
    Column('revision', BigInteger, nullable=False, server_default='0'), primary=('tenant_id',))

KNOWLEDGE_BASES = _table('knowledge_bases', _identity('tenant_id', principal=True),
    _identity('kb_id'), Column('name', Text, nullable=False), _identity('owner_id', principal=True),
    Column('revision', BigInteger, nullable=False, server_default='0'), _identity('manifest_id', nullable=True),
    primary=('tenant_id', 'kb_id'))

MEMBERS = _table('members', _identity('tenant_id', principal=True), _identity('kb_id'),
    _identity('principal_id', principal=True), Column('capabilities', TypedJSON, nullable=False),
    primary=('tenant_id', 'kb_id', 'principal_id'), constraints=(ForeignKeyConstraint(
        ('tenant_id', 'kb_id'), ('knowledge_bases.tenant_id', 'knowledge_bases.kb_id')),))

SOURCES = _table('sources', _identity('tenant_id', principal=True), _identity('source_id'),
    _identity('owner_id', principal=True), Column('readers', TypedJSON, nullable=False),
    Column('policy', TypedJSON, nullable=False), Column('revision', BigInteger, nullable=False, server_default='0'),
    primary=('tenant_id', 'source_id'))

SOURCE_VERSIONS = _table('source_versions', _identity('tenant_id', principal=True),
    _identity('source_id'), _identity('source_version_id'), Column('filename', Text, nullable=False),
    Column('original_path', Text, nullable=False), Column('original_sha256', ExactString(64), nullable=False),
    Column('media_type', Text, nullable=False), primary=('tenant_id', 'source_id', 'source_version_id'),
    constraints=(ForeignKeyConstraint(('tenant_id', 'source_id'), ('sources.tenant_id', 'sources.source_id')),))

PARSE_SNAPSHOTS = _table('parse_snapshots', _identity('tenant_id', principal=True),
    _identity('source_id'), _identity('source_version_id'), _identity('parse_snapshot_id'),
    Column('document', TypedJSON, nullable=False),
    primary=('tenant_id', 'source_id', 'source_version_id', 'parse_snapshot_id'),
    constraints=(ForeignKeyConstraint(('tenant_id', 'source_id', 'source_version_id'),
        ('source_versions.tenant_id', 'source_versions.source_id', 'source_versions.source_version_id')),))

BINDINGS = _table('bindings', _identity('tenant_id', principal=True), _identity('kb_id'),
    _identity('binding_id'), _identity('source_id'), _identity('source_version_id'), _identity('parse_snapshot_id'),
    Column('status', ExactString(16), nullable=False),
    Column('revision', BigInteger, nullable=False, server_default='0'),
    primary=('tenant_id', 'kb_id', 'binding_id'), scope=('tenant_id', 'kb_id'), constraints=(
        UniqueConstraint('tenant_id', 'kb_id', 'source_id'),
        CheckConstraint(Column('status').in_(('ACTIVE', 'REMOVED', 'SUSPENDED'))),
        ForeignKeyConstraint(('tenant_id', 'kb_id'), ('knowledge_bases.tenant_id', 'knowledge_bases.kb_id')),
        ForeignKeyConstraint(('tenant_id', 'source_id', 'source_version_id', 'parse_snapshot_id'),
            ('parse_snapshots.tenant_id', 'parse_snapshots.source_id', 'parse_snapshots.source_version_id',
             'parse_snapshots.parse_snapshot_id'))))

BUILDS = _table('builds', _identity('tenant_id', principal=True), _identity('kb_id'),
    _identity('build_id'), _identity('expected_manifest_id', nullable=True),
    Column('authority_revision', BigInteger, nullable=False), Column('kb_revision', BigInteger, nullable=False),
    Column('index_name', ExactString(128), nullable=False), Column('inputs', TypedJSON, nullable=False),
    Column('status', ExactString(16), nullable=False), Column('unit_count', Integer, nullable=False),
    primary=('tenant_id', 'kb_id', 'build_id'), scope=('tenant_id', 'kb_id'), constraints=(
        CheckConstraint(Column('status').in_(('STAGING', 'VALIDATED', 'PUBLISHED', 'FAILED'))),
        ForeignKeyConstraint(('tenant_id', 'kb_id'), ('knowledge_bases.tenant_id', 'knowledge_bases.kb_id'))))

CITATIONS = _table('citations', _identity('tenant_id', principal=True), _identity('kb_id'),
    _identity('citation_id'), _identity('build_id'), _identity('binding_id'), _identity('source_id'),
    _identity('source_version_id'), _identity('parse_snapshot_id'), _identity('element_id'),
    Column('text', Text, nullable=False), Column('evidence_ids', TypedJSON, nullable=False),
    Column('anchors', TypedJSON, nullable=False), Column('table_data', TypedJSON),
    primary=('tenant_id', 'kb_id', 'citation_id'), scope=('tenant_id', 'kb_id'), constraints=(
        ForeignKeyConstraint(('tenant_id', 'kb_id', 'build_id'),
            ('builds.tenant_id', 'builds.kb_id', 'builds.build_id')),
        ForeignKeyConstraint(('tenant_id', 'kb_id', 'binding_id'),
            ('bindings.tenant_id', 'bindings.kb_id', 'bindings.binding_id')),
        ForeignKeyConstraint(('tenant_id', 'source_id', 'source_version_id', 'parse_snapshot_id'),
            ('parse_snapshots.tenant_id', 'parse_snapshots.source_id', 'parse_snapshots.source_version_id',
             'parse_snapshots.parse_snapshot_id'))))

EMBEDDING_JOBS = _table('embedding_jobs', _identity('tenant_id', principal=True), _identity('kb_id'),
    _identity('build_id'), Column('batch_no', Integer, nullable=False),
    Column('dependency_hash', ExactString(64), nullable=False), Column('provider_hash', ExactString(64), nullable=False),
    Column('payload', TypedJSON, nullable=False), Column('status', ExactString(16), nullable=False, server_default='PENDING'),
    Column('attempt', Integer, nullable=False, server_default='0'), _identity('request_id', nullable=True),
    Column('lease_until', UtcDateTime), Column('retry_at', UtcDateTime), Column('vectors', TypedJSON),
    Column('vectors_hash', ExactString(64)), Column('error', Text),
    primary=('tenant_id', 'kb_id', 'build_id', 'batch_no'), scope=('tenant_id', 'kb_id'), constraints=(
        CheckConstraint(Column('status').in_(('PENDING', 'RUNNING', 'COMPLETE', 'RETRY', 'ERROR', 'UNKNOWN'))),))

EMBEDDING_ATTEMPTS = _table('embedding_attempts', _identity('tenant_id', principal=True), _identity('kb_id'),
    _identity('build_id'), Column('batch_no', Integer, nullable=False), _identity('request_id'),
    Column('status', ExactString(16), nullable=False), Column('diagnostic', Text),
    Column('created_at', UtcDateTime, nullable=False, server_default=Clock()),
    primary=('tenant_id', 'kb_id', 'request_id'), scope=('tenant_id', 'kb_id'), constraints=(
        CheckConstraint(Column('status').in_(('RUNNING', 'COMPLETE', 'ERROR', 'UNKNOWN'))),))

EMBEDDING_CIRCUITS = _table('embedding_circuits', _identity('tenant_id', principal=True), _identity('kb_id'),
    Column('provider_hash', ExactString(64), nullable=False),
    Column('failures', Integer, nullable=False, server_default='0'), Column('open_until', UtcDateTime),
    primary=('tenant_id', 'kb_id', 'provider_hash'), scope=('tenant_id', 'kb_id'))


def knowledge_schema(owner):
    return StructuredSchema(owner, METADATA,
        appendonly=('source_versions', 'parse_snapshots', 'citations'))
