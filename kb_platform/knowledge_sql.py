from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re

from .database_contracts import QueryResult, SqlStatement
from .knowledge_queries import KnowledgeQuery
from .session_identity import PlatformError


@dataclass(frozen=True)
class Operation:
    sql: str
    parameters: tuple[str, ...]


OPERATIONS = {
    KnowledgeQuery.AUTHORITY_BUMP: Operation('UPDATE tenant_state SET revision=revision+1 WHERE tenant_id=:tenant_id', ('tenant_id',)),
    KnowledgeQuery.KB_BUMP: Operation('UPDATE knowledge_bases SET revision=revision+1 WHERE tenant_id=:tenant_id AND kb_id=:kb_id', ('tenant_id', 'kb_id')),
    KnowledgeQuery.AUTHORITY_ENSURE: Operation('INSERT INTO tenant_state (tenant_id) VALUES (:tenant_id) ON CONFLICT DO NOTHING', ('tenant_id',)),
    KnowledgeQuery.KB_INSERT: Operation('INSERT INTO knowledge_bases (tenant_id,kb_id,name,owner_id) VALUES (:tenant_id,:kb_id,:name,:owner_id)', ('tenant_id', 'kb_id', 'name', 'owner_id')),
    KnowledgeQuery.MEMBER_INSERT: Operation('INSERT INTO members VALUES (:tenant_id,:kb_id,:principal_id,:capabilities)', ('tenant_id', 'kb_id', 'principal_id', 'capabilities')),
    KnowledgeQuery.MEMBER_UPSERT: Operation('INSERT INTO members VALUES (:tenant_id,:kb_id,:principal_id,:capabilities) ON CONFLICT (tenant_id,kb_id,principal_id) DO UPDATE SET capabilities=EXCLUDED.capabilities', ('tenant_id', 'kb_id', 'principal_id', 'capabilities')),
    KnowledgeQuery.SOURCE_VERSION_INSERT: Operation('INSERT INTO source_versions VALUES (:tenant_id,:source_id,:source_version_id,:filename,:original_path,:original_sha256,:media_type) ON CONFLICT DO NOTHING', ('tenant_id', 'source_id', 'source_version_id', 'filename', 'original_path', 'original_sha256', 'media_type')),
    KnowledgeQuery.PARSE_INSERT: Operation('INSERT INTO parse_snapshots VALUES (:tenant_id,:source_id,:source_version_id,:parse_snapshot_id,:document) ON CONFLICT DO NOTHING', ('tenant_id', 'source_id', 'source_version_id', 'parse_snapshot_id', 'document')),
    KnowledgeQuery.SOURCE_READERS_UPDATE: Operation('UPDATE sources SET readers=:readers,revision=revision+1 WHERE tenant_id=:tenant_id AND source_id=:source_id', ('readers', 'tenant_id', 'source_id')),
    KnowledgeQuery.SOURCE_POLICY_UPDATE: Operation('UPDATE sources SET policy=:policy,revision=revision+1 WHERE tenant_id=:tenant_id AND source_id=:source_id', ('policy', 'tenant_id', 'source_id')),
    KnowledgeQuery.BUILD_INSERT: Operation("INSERT INTO builds VALUES (:tenant_id,:kb_id,:build_id,:expected_manifest_id,:authority_revision,:kb_revision,:index_name,:inputs,'STAGING',:unit_count)", ('tenant_id', 'kb_id', 'build_id', 'expected_manifest_id', 'authority_revision', 'kb_revision', 'index_name', 'inputs', 'unit_count')),
    KnowledgeQuery.BUILD_VALIDATE: Operation("UPDATE builds SET status='VALIDATED' WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id", ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.BUILD_VALIDATE_STAGED: Operation("UPDATE builds SET status='VALIDATED' WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND status='STAGING'", ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.MANIFEST_UPDATE: Operation('UPDATE knowledge_bases SET manifest_id=:manifest_id WHERE tenant_id=:tenant_id AND kb_id=:kb_id', ('manifest_id', 'tenant_id', 'kb_id')),
    KnowledgeQuery.BUILD_PUBLISH: Operation("UPDATE builds SET status='PUBLISHED' WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id", ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.KB_MEMBERSHIP: Operation('SELECT k.*, m.capabilities FROM knowledge_bases k JOIN members m USING (tenant_id,kb_id) WHERE k.tenant_id=:tenant_id AND k.kb_id=:kb_id AND m.principal_id=:actor_principal_id', ('tenant_id', 'kb_id', 'actor_principal_id')),
    KnowledgeQuery.AUTHORITY_LOCK: Operation('SELECT revision FROM tenant_state WHERE tenant_id=:tenant_id FOR UPDATE', ('tenant_id',)),
    KnowledgeQuery.AUTHORITY_REVISION: Operation('SELECT revision FROM tenant_state WHERE tenant_id=:tenant_id', ('tenant_id',)),
    KnowledgeQuery.SOURCE_GET: Operation('SELECT * FROM sources WHERE tenant_id=:tenant_id AND source_id=:source_id', ('tenant_id', 'source_id')),
    KnowledgeQuery.SOURCE_UPDATE: Operation('UPDATE sources SET readers=:readers,policy=:policy,revision=revision+1 WHERE tenant_id=:tenant_id AND source_id=:source_id', ('readers', 'policy', 'tenant_id', 'source_id')),
    KnowledgeQuery.SOURCE_INSERT: Operation('INSERT INTO sources (tenant_id,source_id,owner_id,readers,policy) VALUES (:tenant_id,:source_id,:owner_id,:readers,:policy)', ('tenant_id', 'source_id', 'owner_id', 'readers', 'policy')),
    KnowledgeQuery.BINDING_UPSERT: Operation("INSERT INTO bindings (tenant_id,kb_id,binding_id,source_id,source_version_id,parse_snapshot_id,status) VALUES (:tenant_id,:kb_id,:binding_id,:source_id,:source_version_id,:parse_snapshot_id,'ACTIVE') ON CONFLICT (tenant_id,kb_id,source_id) DO UPDATE SET source_version_id=EXCLUDED.source_version_id,parse_snapshot_id=EXCLUDED.parse_snapshot_id,status='ACTIVE',revision=bindings.revision+1 RETURNING binding_id,source_id,source_version_id", ('tenant_id', 'kb_id', 'binding_id', 'source_id', 'source_version_id', 'parse_snapshot_id')),
    KnowledgeQuery.BINDINGS_ACTIVE: Operation("SELECT b.*,s.owner_id,s.readers,s.policy,s.revision AS source_revision,p.document FROM bindings b JOIN sources s USING (tenant_id,source_id) JOIN parse_snapshots p USING (tenant_id,source_id,source_version_id,parse_snapshot_id) WHERE b.tenant_id=:tenant_id AND b.kb_id=:kb_id AND b.status='ACTIVE' ORDER BY b.binding_id", ('tenant_id', 'kb_id')),
    KnowledgeQuery.CITATION_INSERT: Operation('INSERT INTO citations VALUES (:tenant_id,:kb_id,:citation_id,:build_id,:binding_id,:source_id,:source_version_id,:parse_snapshot_id,:element_id,:text,:evidence_ids,:anchors,:table_data)', ('tenant_id', 'kb_id', 'citation_id', 'build_id', 'binding_id', 'source_id', 'source_version_id', 'parse_snapshot_id', 'element_id', 'text', 'evidence_ids', 'anchors', 'table_data')),
    KnowledgeQuery.KBS_VISIBLE: Operation("SELECT k.kb_id,k.name,k.manifest_id,m.capabilities FROM knowledge_bases k JOIN members m USING (tenant_id,kb_id) WHERE k.tenant_id=:tenant_id AND m.principal_id=:actor_principal_id AND m.capabilities<>'[]' ORDER BY k.name,k.kb_id", ('tenant_id', 'actor_principal_id')),
    KnowledgeQuery.MEMBER_GET: Operation('SELECT capabilities FROM members WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND principal_id=:principal_id', ('tenant_id', 'kb_id', 'principal_id')),
    KnowledgeQuery.PARSE_FIRST: Operation('SELECT parse_snapshot_id FROM parse_snapshots WHERE tenant_id=:tenant_id AND source_id=:source_id AND source_version_id=:source_version_id ORDER BY parse_snapshot_id LIMIT 1', ('tenant_id', 'source_id', 'source_version_id')),
    KnowledgeQuery.BINDING_REMOVE: Operation("UPDATE bindings SET status='REMOVED',revision=revision+1 WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND binding_id=:binding_id RETURNING binding_id,status", ('tenant_id', 'kb_id', 'binding_id')),
    KnowledgeQuery.BUILD_FAIL: Operation("UPDATE builds SET status='FAILED' WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id", ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.BUILD_STATUS: Operation('SELECT build_id,status,unit_count FROM builds WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id', ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.BUILD_GET: Operation('SELECT * FROM builds WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id', ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.CITATIONS_FOR_BUILD: Operation('SELECT * FROM citations WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id', ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.BUILD_LOCK: Operation('SELECT * FROM builds WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id FOR UPDATE', ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.BUILD_PUBLISHED: Operation("SELECT * FROM builds WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND status='PUBLISHED'", ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.CITATION_RESOLVE: Operation("SELECT c.*,p.document,v.filename,v.original_path,v.original_sha256,v.media_type,s.policy,s.owner_id,s.readers FROM citations c JOIN bindings b USING (tenant_id,kb_id,binding_id) JOIN builds d ON c.tenant_id=d.tenant_id AND c.kb_id=d.kb_id AND c.build_id=d.build_id JOIN source_versions v ON c.tenant_id=v.tenant_id AND c.source_id=v.source_id AND c.source_version_id=v.source_version_id JOIN sources s ON c.tenant_id=s.tenant_id AND c.source_id=s.source_id JOIN parse_snapshots p ON c.tenant_id=p.tenant_id AND c.source_id=p.source_id AND c.source_version_id=p.source_version_id AND c.parse_snapshot_id=p.parse_snapshot_id WHERE c.tenant_id=:tenant_id AND c.kb_id=:kb_id AND c.citation_id=:citation_id AND b.source_id=c.source_id AND b.status='ACTIVE' AND d.status='PUBLISHED'", ('tenant_id', 'kb_id', 'citation_id')),
    KnowledgeQuery.CITATION_GET: Operation('SELECT * FROM citations WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND citation_id=:citation_id AND build_id=:build_id', ('tenant_id', 'kb_id', 'citation_id', 'build_id')),
    KnowledgeQuery.CITATION_CANONICAL: Operation('SELECT c.element_id,p.document FROM citations c JOIN parse_snapshots p USING (tenant_id,source_id,source_version_id,parse_snapshot_id) WHERE c.tenant_id=:tenant_id AND c.kb_id=:kb_id AND c.citation_id=:citation_id AND c.build_id=:build_id AND c.source_id=:source_id AND c.source_version_id=:source_version_id AND c.parse_snapshot_id=:parse_snapshot_id', ('tenant_id', 'kb_id', 'citation_id', 'build_id', 'source_id', 'source_version_id', 'parse_snapshot_id')),
    KnowledgeQuery.BINDINGS_PROCESSING: Operation("SELECT b.*,s.owner_id,s.readers,s.policy,v.filename,p.document FROM bindings b JOIN sources s USING (tenant_id,source_id) JOIN parse_snapshots p USING (tenant_id,source_id,source_version_id,parse_snapshot_id) JOIN source_versions v USING (tenant_id,source_id,source_version_id) WHERE b.tenant_id=:tenant_id AND b.kb_id=:kb_id AND b.status='ACTIVE' ORDER BY b.binding_id", ('tenant_id', 'kb_id')),
    KnowledgeQuery.SOURCE_VERSION_ORIGINAL: Operation('SELECT original_path,original_sha256 FROM source_versions WHERE tenant_id=:tenant_id AND source_id=:source_id AND source_version_id=:source_version_id', ('tenant_id', 'source_id', 'source_version_id')),
}

OPERATIONS.update({
    KnowledgeQuery.JOB_CLOCK: Operation('SELECT CURRENT_TIMESTAMP AS db_now', ()),
    KnowledgeQuery.JOBS_INPUTS: Operation(
        'SELECT dependency_hash,payload FROM embedding_jobs WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id',
        ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.JOB_INSERT: Operation(
        'INSERT INTO embedding_jobs (tenant_id,kb_id,build_id,batch_no,dependency_hash,provider_hash,payload) '
        'VALUES (:tenant_id,:kb_id,:build_id,:batch_no,:dependency_hash,:provider_hash,:payload) ON CONFLICT DO NOTHING',
        ('tenant_id', 'kb_id', 'build_id', 'batch_no', 'dependency_hash', 'provider_hash', 'payload')),
    KnowledgeQuery.CIRCUIT_ENSURE: Operation(
        'INSERT INTO embedding_circuits (tenant_id,kb_id,provider_hash) '
        'VALUES (:tenant_id,:kb_id,:provider_hash) ON CONFLICT DO NOTHING',
        ('tenant_id', 'kb_id', 'provider_hash')),
    KnowledgeQuery.JOB_LOCK: Operation(
        'SELECT * FROM embedding_jobs WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND batch_no=:batch_no FOR UPDATE',
        ('tenant_id', 'kb_id', 'build_id', 'batch_no')),
    KnowledgeQuery.JOB_EXPIRE: Operation(
        "UPDATE embedding_jobs SET status='UNKNOWN',error='LEASE_EXPIRED' WHERE "
        'tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND batch_no=:batch_no',
        ('tenant_id', 'kb_id', 'build_id', 'batch_no')),
    KnowledgeQuery.ATTEMPT_EXPIRE: Operation(
        "UPDATE embedding_attempts SET status='UNKNOWN',diagnostic='LEASE_EXPIRED' WHERE "
        "tenant_id=:tenant_id AND kb_id=:kb_id AND request_id=:request_id AND status='RUNNING'",
        ('tenant_id', 'kb_id', 'request_id')),
    KnowledgeQuery.CIRCUIT_LOCK: Operation(
        'SELECT * FROM embedding_circuits WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND provider_hash=:provider_hash FOR UPDATE',
        ('tenant_id', 'kb_id', 'provider_hash')),
    KnowledgeQuery.JOB_CLAIM: Operation(
        "UPDATE embedding_jobs SET status='RUNNING',attempt=attempt+1-"
        'CASE WHEN :old_preflight AND attempt>0 THEN 1 ELSE 0 END,'
        'request_id=:request_id,lease_until=:lease_until,retry_at=NULL '
        'WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND batch_no=:batch_no RETURNING *',
        ('old_preflight', 'request_id', 'lease_seconds', 'tenant_id', 'kb_id', 'build_id', 'batch_no')),
    KnowledgeQuery.ATTEMPT_INSERT: Operation(
        'INSERT INTO embedding_attempts (tenant_id,kb_id,build_id,batch_no,request_id,status) '
        "VALUES (:tenant_id,:kb_id,:build_id,:batch_no,:request_id,'RUNNING')",
        ('tenant_id', 'kb_id', 'build_id', 'batch_no', 'request_id')),
    KnowledgeQuery.JOB_FINISH: Operation(
        'UPDATE embedding_jobs SET status=:status,vectors=:vectors,vectors_hash=:vectors_hash,error=:error,'
        'attempt=attempt-CASE WHEN :retry_without_attempt AND attempt>0 THEN 1 ELSE 0 END,retry_at=:retry_at '
        'WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND batch_no=:batch_no '
        'AND request_id=:request_id AND (status=:expected_status OR status=:alternate_status) '
        'AND (:ignore_lease OR lease_until>=:db_now) RETURNING retry_at',
        ('status', 'vectors', 'vectors_hash', 'error', 'retry_without_attempt', 'retry_seconds',
         'tenant_id', 'kb_id', 'build_id', 'batch_no', 'request_id', 'expected_status', 'alternate_status', 'ignore_lease')),
    KnowledgeQuery.ATTEMPT_FINISH: Operation(
        'UPDATE embedding_attempts SET status=:status,diagnostic=:diagnostic WHERE '
        'tenant_id=:tenant_id AND kb_id=:kb_id AND request_id=:request_id',
        ('status', 'diagnostic', 'tenant_id', 'kb_id', 'request_id')),
    KnowledgeQuery.CIRCUIT_FAILURE: Operation(
        'UPDATE embedding_circuits SET failures=failures+1,open_until=CASE WHEN '
        'failures+1>=:threshold THEN :open_until ELSE open_until END '
        'WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND provider_hash=:provider_hash',
        ('threshold', 'circuit_seconds', 'tenant_id', 'kb_id', 'provider_hash')),
    KnowledgeQuery.CIRCUIT_RESET: Operation(
        'UPDATE embedding_circuits SET failures=0,open_until=NULL WHERE '
        'tenant_id=:tenant_id AND kb_id=:kb_id AND provider_hash=:provider_hash',
        ('tenant_id', 'kb_id', 'provider_hash')),
    KnowledgeQuery.JOBS_STATUS: Operation(
        'SELECT batch_no,status,attempt,request_id,lease_until,retry_at,error FROM embedding_jobs '
        'WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id ORDER BY batch_no',
        ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.JOBS_EXPIRE: Operation(
        "UPDATE embedding_jobs SET status='UNKNOWN',error='LEASE_EXPIRED' WHERE "
        "tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND status='RUNNING' AND lease_until<:db_now",
        ('tenant_id', 'kb_id', 'build_id')),
    KnowledgeQuery.JOBS_UNKNOWN: Operation(
        "SELECT * FROM embedding_jobs WHERE tenant_id=:tenant_id AND kb_id=:kb_id AND build_id=:build_id AND status='UNKNOWN'",
        ('tenant_id', 'kb_id', 'build_id')),
})

JSON_FIELDS = frozenset({'capabilities', 'readers', 'policy', 'document', 'inputs',
                         'evidence_ids', 'anchors', 'table_data', 'payload', 'vectors'})
TIME_FIELDS = frozenset({'lease_until', 'retry_at', 'open_until', 'created_at', 'db_now'})
IMMUTABLE_LOOKUPS = {
    KnowledgeQuery.SOURCE_VERSION_INSERT:
        'SELECT * FROM source_versions WHERE tenant_id=:tenant_id AND source_id=:source_id AND source_version_id=:source_version_id',
    KnowledgeQuery.PARSE_INSERT:
        'SELECT * FROM parse_snapshots WHERE tenant_id=:tenant_id AND source_id=:source_id AND source_version_id=:source_version_id AND parse_snapshot_id=:parse_snapshot_id',
}


def operation_parameters(query, params):
    if not isinstance(query, KnowledgeQuery):
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


def _datetime(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _deadline(engine, parameter):
    if engine == 'postgresql':
        return "clock_timestamp()+CAST(:" + parameter + " AS double precision)*interval '1 second'"
    return "strftime('%Y-%m-%dT%H:%M:%f+00:00','now',:" + parameter + " || ' seconds')"


def execute_operation(transaction, engine, query, values, *, write):
    operation = OPERATIONS[query]
    sql = operation.sql
    if not write and (not sql.startswith('SELECT ') or sql.endswith(' FOR UPDATE')):
        raise ValueError('KNOWLEDGE_WRITE_TRANSACTION_REQUIRED')
    original = dict(values)
    values = dict(values)
    for key in ('lease_seconds', 'retry_seconds', 'circuit_seconds'):
        if key in values and values[key] is not None:
            if (type(values[key]) not in (int, float) or not math.isfinite(values[key])
                    or values[key] < 0 or key != 'retry_seconds' and values[key] == 0):
                raise ValueError('INVALID_KNOWLEDGE_DURATION')
    clock = 'clock_timestamp()' if engine == 'postgresql' else "julianday('now')"
    lease = 'lease_until' if engine == 'postgresql' else 'julianday(lease_until)'
    if query == KnowledgeQuery.JOB_CLAIM:
        sql = sql.replace(':lease_until', _deadline(engine, 'lease_seconds'))
    elif query == KnowledgeQuery.JOB_FINISH:
        lock = OPERATIONS[KnowledgeQuery.JOB_LOCK].sql
        if engine == 'sqlite':
            lock = lock.removesuffix(' FOR UPDATE')
        scope = {key: values[key] for key in ('tenant_id', 'kb_id', 'build_id', 'batch_no')}
        transaction.execute(SqlStatement(lock, scope))
        delay = ('CAST(:retry_seconds AS double precision)' if engine == 'postgresql' else ':retry_seconds')
        sql = sql.replace('retry_at=:retry_at', 'retry_at=CASE WHEN ' + delay + ' IS NULL THEN NULL ELSE '
                          + _deadline(engine, 'retry_seconds') + ' END')
        sql = sql.replace('lease_until>=:db_now', lease + '>=' + clock)
    elif query == KnowledgeQuery.CIRCUIT_FAILURE:
        sql = sql.replace(':open_until', _deadline(engine, 'circuit_seconds'))
    elif query == KnowledgeQuery.JOBS_EXPIRE:
        sql = sql.replace('lease_until<:db_now', lease + '<' + clock)
    if engine == 'sqlite':
        sql = sql.removesuffix(' FOR UPDATE')
        if query == KnowledgeQuery.JOB_CLOCK:
            sql = "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00','now') AS db_now"
    elif query == KnowledgeQuery.JOB_CLOCK:
        sql = 'SELECT clock_timestamp() AS db_now'
    for key in values.keys() & JSON_FIELDS:
        values[key] = json.dumps(values[key], ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':'), allow_nan=False) if values[key] is not None else None
        if engine == 'postgresql':
            sql = re.sub(':' + re.escape(key) + r'\b', 'CAST(:' + key + ' AS jsonb)', sql)
    for key in values.keys() & TIME_FIELDS:
        if values[key] is not None:
            values[key] = _datetime(values[key]).isoformat(timespec='microseconds')
    result = transaction.execute(SqlStatement(sql, values))
    if query in IMMUTABLE_LOOKUPS:
        stored = transaction.execute(SqlStatement(IMMUTABLE_LOOKUPS[query], values)).fetchone()
        if stored is None:
            raise PlatformError('SOURCE_INTEGRITY', 'Immutable source record is unavailable', 503)
        for key, expected in original.items():
            actual = stored[key]
            if key in JSON_FIELDS and isinstance(actual, str):
                actual = json.loads(actual)
            if actual != expected:
                raise PlatformError('SOURCE_INTEGRITY', 'Immutable source identity has different content', 409)
    rows = []
    for raw in result.rows:
        row = dict(raw)
        for key in row.keys() & JSON_FIELDS:
            if isinstance(row[key], str):
                row[key] = json.loads(row[key])
        for key in row.keys() & TIME_FIELDS:
            if row[key] is not None:
                row[key] = _datetime(row[key])
        rows.append(row)
    return QueryResult(rows, result.rowcount)
