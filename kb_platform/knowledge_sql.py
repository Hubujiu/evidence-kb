from collections.abc import Callable
from dataclasses import dataclass
import math

from sqlalchemy import Boolean, Float, and_, bindparam, case, or_, select, update

from .database_contracts import QueryResult
from .knowledge_queries import KnowledgeQuery as K
from .knowledge_schema import (
    BINDINGS, BUILDS, CITATIONS, EMBEDDING_ATTEMPTS, EMBEDDING_CIRCUITS, EMBEDDING_JOBS,
    KNOWLEDGE_BASES, MEMBERS, PARSE_SNAPSHOTS, SOURCES, SOURCE_VERSIONS, TENANTS,
)
from .session_identity import PlatformError
from .storage_sdk.relational import After, Clock


@dataclass(frozen=True)
class Operation:
    parameters: tuple[str, ...]
    run: Callable
    write: bool = False


def _parameter(name, **options):
    return bindparam('p_' + name, **options)


def _where(table, *keys):
    return and_(*(table.c[key] == _parameter(key) for key in keys))


def _join(left, right, *keys):
    return left.join(right, and_(*(left.c[key] == right.c[key] for key in keys)))


def _statement(parameters, statement, *, write=False):
    return Operation(tuple(parameters), lambda tx, values: tx.execute(statement, {'p_' + key: value for key, value in values.items()}), write)


def _insert(table, parameters, *, conflict_keys=(), updates=None, returning=(), defaults=None):
    def run(transaction, values):
        inserted = {**values, **({} if defaults is None else defaults)}
        changes = updates(values) if updates is not None else None
        return transaction.insert(table, inserted, conflict_keys=conflict_keys,
                                  updates=changes, returning=returning)

    return Operation(tuple(parameters), run, True)


TENANT = ('tenant_id',)
KB = (*TENANT, 'kb_id')
SOURCE = (*TENANT, 'source_id')
VERSION = (*SOURCE, 'source_version_id')
PARSE = (*VERSION, 'parse_snapshot_id')
BUILD = (*KB, 'build_id')
BATCH = (*BUILD, 'batch_no')
CIRCUIT = (*KB, 'provider_hash')
ATTEMPT = (*KB, 'request_id')


bindings_source = _join(BINDINGS, SOURCES, *SOURCE)
bindings_parse = bindings_source.join(PARSE_SNAPSHOTS,
    and_(*(BINDINGS.c[key] == PARSE_SNAPSHOTS.c[key] for key in PARSE)))
bindings_versions = bindings_parse.join(SOURCE_VERSIONS,
    and_(*(BINDINGS.c[key] == SOURCE_VERSIONS.c[key] for key in VERSION)))
citations_parse = _join(CITATIONS, PARSE_SNAPSHOTS, *PARSE)
citations_binding = _join(CITATIONS, BINDINGS, *KB, 'binding_id')
citations_build = citations_binding.join(BUILDS,
    and_(*(CITATIONS.c[key] == BUILDS.c[key] for key in BUILD)))
citations_version = citations_build.join(SOURCE_VERSIONS,
    and_(*(CITATIONS.c[key] == SOURCE_VERSIONS.c[key] for key in VERSION)))
citations_source = citations_version.join(SOURCES,
    and_(*(CITATIONS.c[key] == SOURCES.c[key] for key in SOURCE)))
citations_all = citations_source.join(PARSE_SNAPSHOTS,
    and_(*(CITATIONS.c[key] == PARSE_SNAPSHOTS.c[key] for key in PARSE)))


OPERATIONS = {
    K.AUTHORITY_BUMP: _statement(TENANT,
        update(TENANTS).where(_where(TENANTS, *TENANT)).values(revision=TENANTS.c.revision + 1), write=True),
    K.KB_BUMP: _statement(KB,
        update(KNOWLEDGE_BASES).where(_where(KNOWLEDGE_BASES, *KB))
        .values(revision=KNOWLEDGE_BASES.c.revision + 1), write=True),
    K.AUTHORITY_ENSURE: _insert(TENANTS, TENANT, conflict_keys=TENANT),
    K.KB_INSERT: _insert(KNOWLEDGE_BASES, (*KB, 'name', 'owner_id')),
    K.MEMBER_INSERT: _insert(MEMBERS, (*KB, 'principal_id', 'capabilities')),
    K.MEMBER_UPSERT: _insert(MEMBERS, (*KB, 'principal_id', 'capabilities'),
        conflict_keys=(*KB, 'principal_id'), updates=lambda values: {'capabilities': values['capabilities']}),
    K.SOURCE_VERSION_INSERT: _insert(SOURCE_VERSIONS,
        (*VERSION, 'filename', 'original_path', 'original_sha256', 'media_type'), conflict_keys=VERSION),
    K.PARSE_INSERT: _insert(PARSE_SNAPSHOTS, (*PARSE, 'document'), conflict_keys=PARSE),
    K.SOURCE_READERS_UPDATE: _statement(('readers', *SOURCE),
        update(SOURCES).where(_where(SOURCES, *SOURCE))
        .values(readers=_parameter('readers'), revision=SOURCES.c.revision + 1), write=True),
    K.SOURCE_POLICY_UPDATE: _statement(('policy', *SOURCE),
        update(SOURCES).where(_where(SOURCES, *SOURCE))
        .values(policy=_parameter('policy'), revision=SOURCES.c.revision + 1), write=True),
    K.BUILD_INSERT: _insert(BUILDS, (*BUILD, 'expected_manifest_id', 'authority_revision',
        'kb_revision', 'index_name', 'inputs', 'unit_count'), defaults={'status': 'STAGING'}),
    K.BUILD_VALIDATE: _statement(BUILD,
        update(BUILDS).where(_where(BUILDS, *BUILD)).values(status='VALIDATED'), write=True),
    K.BUILD_VALIDATE_STAGED: _statement(BUILD,
        update(BUILDS).where(_where(BUILDS, *BUILD), BUILDS.c.status == 'STAGING')
        .values(status='VALIDATED'), write=True),
    K.MANIFEST_UPDATE: _statement(('manifest_id', *KB),
        update(KNOWLEDGE_BASES).where(_where(KNOWLEDGE_BASES, *KB))
        .values(manifest_id=_parameter('manifest_id')), write=True),
    K.BUILD_PUBLISH: _statement(BUILD,
        update(BUILDS).where(_where(BUILDS, *BUILD)).values(status='PUBLISHED'), write=True),
    K.KB_MEMBERSHIP: _statement((*KB, 'actor_principal_id'),
        select(KNOWLEDGE_BASES, MEMBERS.c.capabilities).select_from(_join(KNOWLEDGE_BASES, MEMBERS, *KB))
        .where(_where(KNOWLEDGE_BASES, *KB), MEMBERS.c.principal_id == _parameter('actor_principal_id'))),
    K.AUTHORITY_LOCK: _statement(TENANT,
        select(TENANTS.c.revision).where(_where(TENANTS, *TENANT)).with_for_update(), write=True),
    K.AUTHORITY_REVISION: _statement(TENANT,
        select(TENANTS.c.revision).where(_where(TENANTS, *TENANT))),
    K.SOURCE_GET: _statement(SOURCE, select(SOURCES).where(_where(SOURCES, *SOURCE))),
    K.SOURCE_UPDATE: _statement(('readers', 'policy', *SOURCE),
        update(SOURCES).where(_where(SOURCES, *SOURCE)).values(readers=_parameter('readers'),
            policy=_parameter('policy'), revision=SOURCES.c.revision + 1), write=True),
    K.SOURCE_INSERT: _insert(SOURCES, (*SOURCE, 'owner_id', 'readers', 'policy')),
    K.BINDING_UPSERT: _insert(BINDINGS, (*KB, 'binding_id', 'source_id', 'source_version_id', 'parse_snapshot_id'),
        conflict_keys=(*KB, 'source_id'), defaults={'status': 'ACTIVE'},
        updates=lambda values: {'source_version_id': values['source_version_id'],
            'parse_snapshot_id': values['parse_snapshot_id'], 'status': 'ACTIVE',
            'revision': BINDINGS.c.revision + 1},
        returning=(BINDINGS.c.binding_id, BINDINGS.c.source_id, BINDINGS.c.source_version_id)),
    K.BINDINGS_ACTIVE: _statement(KB,
        select(BINDINGS, SOURCES.c.owner_id, SOURCES.c.readers, SOURCES.c.policy,
            SOURCES.c.revision.label('source_revision'), PARSE_SNAPSHOTS.c.document)
        .select_from(bindings_parse).where(_where(BINDINGS, *KB), BINDINGS.c.status == 'ACTIVE')
        .order_by(BINDINGS.c.binding_id)),
    K.BINDING_ACCESS: _statement((*KB, 'source_id'),
        select(BINDINGS).where(_where(BINDINGS, *KB, 'source_id'), BINDINGS.c.status == 'ACTIVE')),
    K.CITATION_INSERT: _insert(CITATIONS, (*KB, 'citation_id', 'build_id', 'binding_id', 'source_id',
        'source_version_id', 'parse_snapshot_id', 'element_id', 'text', 'evidence_ids', 'anchors', 'table_data')),
    K.KBS_VISIBLE: _statement((*TENANT, 'actor_principal_id'),
        select(KNOWLEDGE_BASES.c.kb_id, KNOWLEDGE_BASES.c.name, KNOWLEDGE_BASES.c.manifest_id, MEMBERS.c.capabilities)
        .select_from(_join(KNOWLEDGE_BASES, MEMBERS, *KB))
        .where(_where(KNOWLEDGE_BASES, *TENANT), MEMBERS.c.principal_id == _parameter('actor_principal_id'))
        .order_by(KNOWLEDGE_BASES.c.name, KNOWLEDGE_BASES.c.kb_id)),
    K.MEMBER_GET: _statement((*KB, 'principal_id'),
        select(MEMBERS.c.capabilities).where(_where(MEMBERS, *KB, 'principal_id'))),
    K.PARSE_FIRST: _statement(VERSION,
        select(PARSE_SNAPSHOTS.c.parse_snapshot_id).where(_where(PARSE_SNAPSHOTS, *VERSION))
        .order_by(PARSE_SNAPSHOTS.c.parse_snapshot_id).limit(1)),
    K.BINDING_REMOVE: _statement((*KB, 'binding_id'),
        update(BINDINGS).where(_where(BINDINGS, *KB, 'binding_id'))
        .values(status='REMOVED', revision=BINDINGS.c.revision + 1)
        .returning(BINDINGS.c.binding_id, BINDINGS.c.status), write=True),
    K.BUILD_FAIL: _statement(BUILD,
        update(BUILDS).where(_where(BUILDS, *BUILD)).values(status='FAILED'), write=True),
    K.BUILD_STATUS: _statement(BUILD,
        select(BUILDS.c.build_id, BUILDS.c.status, BUILDS.c.unit_count).where(_where(BUILDS, *BUILD))),
    K.BUILD_GET: _statement(BUILD, select(BUILDS).where(_where(BUILDS, *BUILD))),
    K.CITATIONS_FOR_BUILD: _statement(BUILD, select(CITATIONS).where(_where(CITATIONS, *BUILD))),
    K.BUILD_LOCK: _statement(BUILD, select(BUILDS).where(_where(BUILDS, *BUILD)).with_for_update(), write=True),
    K.BUILD_PUBLISHED: _statement(BUILD,
        select(BUILDS).where(_where(BUILDS, *BUILD), BUILDS.c.status == 'PUBLISHED')),
    K.CITATION_RESOLVE: _statement((*KB, 'citation_id'),
        select(CITATIONS, PARSE_SNAPSHOTS.c.document, SOURCE_VERSIONS.c.filename,
            SOURCE_VERSIONS.c.original_path, SOURCE_VERSIONS.c.original_sha256, SOURCE_VERSIONS.c.media_type,
            SOURCES.c.policy, SOURCES.c.owner_id, SOURCES.c.readers).select_from(citations_all)
        .where(_where(CITATIONS, *KB, 'citation_id'), BINDINGS.c.source_id == CITATIONS.c.source_id,
            BINDINGS.c.status == 'ACTIVE', BUILDS.c.status == 'PUBLISHED')),
    K.CITATION_ACCESS: _statement((*KB, 'citation_id'),
        select(CITATIONS, SOURCE_VERSIONS.c.filename, SOURCE_VERSIONS.c.original_path,
            SOURCE_VERSIONS.c.original_sha256, SOURCE_VERSIONS.c.media_type,
            SOURCES.c.policy, SOURCES.c.owner_id, SOURCES.c.readers).select_from(citations_all)
        .where(_where(CITATIONS, *KB, 'citation_id'), BINDINGS.c.source_id == CITATIONS.c.source_id,
            BINDINGS.c.status == 'ACTIVE', BUILDS.c.status == 'PUBLISHED')),
    K.CITATION_GET: _statement((*KB, 'citation_id', 'build_id'),
        select(CITATIONS).where(_where(CITATIONS, *KB, 'citation_id', 'build_id'))),
    K.CITATION_CANONICAL: _statement((*KB, 'citation_id', 'build_id', 'source_id',
                                    'source_version_id', 'parse_snapshot_id'),
        select(CITATIONS.c.element_id, PARSE_SNAPSHOTS.c.document).select_from(citations_parse)
        .where(_where(CITATIONS, *KB, 'citation_id', 'build_id', 'source_id',
                     'source_version_id', 'parse_snapshot_id'))),
    K.BINDINGS_PROCESSING: _statement(KB,
        select(BINDINGS, SOURCES.c.owner_id, SOURCES.c.readers, SOURCES.c.policy,
            SOURCE_VERSIONS.c.filename, PARSE_SNAPSHOTS.c.document).select_from(bindings_versions)
        .where(_where(BINDINGS, *KB), BINDINGS.c.status == 'ACTIVE').order_by(BINDINGS.c.binding_id)),
    K.SOURCE_VERSION_ORIGINAL: _statement(VERSION,
        select(SOURCE_VERSIONS.c.original_path, SOURCE_VERSIONS.c.original_sha256)
        .where(_where(SOURCE_VERSIONS, *VERSION))),
    K.JOB_CLOCK: _statement((), select(Clock().label('db_now'))),
    K.JOBS_INPUTS: _statement(BUILD,
        select(EMBEDDING_JOBS.c.dependency_hash, EMBEDDING_JOBS.c.payload).where(_where(EMBEDDING_JOBS, *BUILD))),
    K.JOB_INSERT: _insert(EMBEDDING_JOBS, (*BATCH, 'dependency_hash', 'provider_hash', 'payload'), conflict_keys=BATCH),
    K.CIRCUIT_ENSURE: _insert(EMBEDDING_CIRCUITS, CIRCUIT, conflict_keys=CIRCUIT),
    K.JOB_LOCK: _statement(BATCH,
        select(EMBEDDING_JOBS).where(_where(EMBEDDING_JOBS, *BATCH)).with_for_update(), write=True),
    K.JOB_EXPIRE: _statement(BATCH,
        update(EMBEDDING_JOBS).where(_where(EMBEDDING_JOBS, *BATCH))
        .values(status='UNKNOWN', error='LEASE_EXPIRED'), write=True),
    K.ATTEMPT_EXPIRE: _statement(ATTEMPT,
        update(EMBEDDING_ATTEMPTS).where(_where(EMBEDDING_ATTEMPTS, *ATTEMPT),
            EMBEDDING_ATTEMPTS.c.status == 'RUNNING').values(status='UNKNOWN', diagnostic='LEASE_EXPIRED'), write=True),
    K.CIRCUIT_LOCK: _statement(CIRCUIT,
        select(EMBEDDING_CIRCUITS).where(_where(EMBEDDING_CIRCUITS, *CIRCUIT)).with_for_update(), write=True),
    K.JOB_CLAIM: _statement(('old_preflight', 'request_id', 'lease_seconds', *BATCH),
        update(EMBEDDING_JOBS).where(_where(EMBEDDING_JOBS, *BATCH)).values(
            status='RUNNING', request_id=_parameter('request_id'),
            attempt=EMBEDDING_JOBS.c.attempt + 1 - case(
                (and_(_parameter('old_preflight', type_=Boolean), EMBEDDING_JOBS.c.attempt > 0), 1), else_=0),
            lease_until=After(_parameter('lease_seconds', type_=Float)), retry_at=None)
        .returning(*EMBEDDING_JOBS.c), write=True),
    K.ATTEMPT_INSERT: _insert(EMBEDDING_ATTEMPTS, (*BATCH, 'request_id'), defaults={'status': 'RUNNING'}),
    K.JOB_FINISH: _statement(('status', 'vectors', 'vectors_hash', 'error', 'retry_without_attempt', 'retry_seconds',
                             *BATCH, 'request_id', 'expected_status', 'alternate_status', 'ignore_lease'),
        update(EMBEDDING_JOBS).where(_where(EMBEDDING_JOBS, *BATCH, 'request_id'),
            or_(EMBEDDING_JOBS.c.status == _parameter('expected_status'),
                EMBEDDING_JOBS.c.status == _parameter('alternate_status')),
            or_(_parameter('ignore_lease', type_=Boolean), EMBEDDING_JOBS.c.lease_until >= Clock()))
        .values(status=_parameter('status'), vectors=_parameter('vectors'), vectors_hash=_parameter('vectors_hash'),
            error=_parameter('error'), attempt=EMBEDDING_JOBS.c.attempt - case(
                (and_(_parameter('retry_without_attempt', type_=Boolean), EMBEDDING_JOBS.c.attempt > 0), 1), else_=0),
            retry_at=After(_parameter('retry_seconds', type_=Float)))
        .returning(EMBEDDING_JOBS.c.retry_at), write=True),
    K.ATTEMPT_FINISH: _statement(('status', 'diagnostic', *ATTEMPT),
        update(EMBEDDING_ATTEMPTS).where(_where(EMBEDDING_ATTEMPTS, *ATTEMPT))
        .values(status=_parameter('status'), diagnostic=_parameter('diagnostic')), write=True),
    K.CIRCUIT_FAILURE: _statement(('threshold', 'circuit_seconds', *CIRCUIT),
        update(EMBEDDING_CIRCUITS).where(_where(EMBEDDING_CIRCUITS, *CIRCUIT)).values(
            failures=EMBEDDING_CIRCUITS.c.failures + 1,
            open_until=case((EMBEDDING_CIRCUITS.c.failures + 1 >= _parameter('threshold'),
                             After(_parameter('circuit_seconds', type_=Float))),
                            else_=EMBEDDING_CIRCUITS.c.open_until)), write=True),
    K.CIRCUIT_RESET: _statement(CIRCUIT,
        update(EMBEDDING_CIRCUITS).where(_where(EMBEDDING_CIRCUITS, *CIRCUIT))
        .values(failures=0, open_until=None), write=True),
    K.JOBS_STATUS: _statement(BUILD,
        select(*(EMBEDDING_JOBS.c[key] for key in (
            'batch_no', 'status', 'attempt', 'request_id', 'lease_until', 'retry_at', 'error')))
        .where(_where(EMBEDDING_JOBS, *BUILD)).order_by(EMBEDDING_JOBS.c.batch_no)),
    K.JOBS_EXPIRE: _statement(BUILD,
        update(EMBEDDING_JOBS).where(_where(EMBEDDING_JOBS, *BUILD),
            EMBEDDING_JOBS.c.status == 'RUNNING', EMBEDDING_JOBS.c.lease_until < Clock())
        .values(status='UNKNOWN', error='LEASE_EXPIRED'), write=True),
    K.JOBS_UNKNOWN: _statement(BUILD,
        select(EMBEDDING_JOBS).where(_where(EMBEDDING_JOBS, *BUILD), EMBEDDING_JOBS.c.status == 'UNKNOWN')),
}


IMMUTABLE_LOOKUPS = {
    K.SOURCE_VERSION_INSERT: select(SOURCE_VERSIONS).where(_where(SOURCE_VERSIONS, *VERSION)),
    K.PARSE_INSERT: select(PARSE_SNAPSHOTS).where(_where(PARSE_SNAPSHOTS, *PARSE)),
}


def operation_parameters(query, params):
    if not isinstance(query, K):
        raise ValueError('UNREGISTERED_KNOWLEDGE_QUERY')
    operation = OPERATIONS[query]
    if isinstance(params, dict):
        if set(params) != set(operation.parameters):
            raise ValueError('INVALID_KNOWLEDGE_PARAMETERS')
        return dict(params)
    values = () if params is None else tuple(params)
    if len(values) != len(operation.parameters):
        raise ValueError('INVALID_KNOWLEDGE_PARAMETERS')
    return dict(zip(operation.parameters, values, strict=True))


def execute_operation(transaction, query, values, *, write):
    operation = OPERATIONS[query]
    if not write and operation.write:
        raise ValueError('KNOWLEDGE_WRITE_TRANSACTION_REQUIRED')
    for key in ('lease_seconds', 'retry_seconds', 'circuit_seconds'):
        if key in values and values[key] is not None:
            if (type(values[key]) not in (int, float) or not math.isfinite(values[key])
                    or values[key] < 0 or key != 'retry_seconds' and values[key] == 0):
                raise ValueError('INVALID_KNOWLEDGE_DURATION')
    if query == K.JOB_FINISH:
        OPERATIONS[K.JOB_LOCK].run(transaction, {key: values[key] for key in BATCH})
    result = operation.run(transaction, values)
    if query in IMMUTABLE_LOOKUPS:
        stored = transaction.execute(IMMUTABLE_LOOKUPS[query], {'p_' + key: value for key, value in values.items()}).fetchone()
        if stored is None:
            raise PlatformError('SOURCE_INTEGRITY', 'Immutable source record is unavailable', 503)
        if any(stored[key] != expected for key, expected in values.items()):
            raise PlatformError('SOURCE_INTEGRITY', 'Immutable source identity has different content', 409)
    if query == K.KBS_VISIBLE:
        rows = [row for row in result.rows if row['capabilities']]
        return QueryResult(rows, len(rows))
    return result
