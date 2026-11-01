from copy import deepcopy
import hashlib
import inspect
import json
import math
from uuid import uuid4

from .knowledge_queries import KnowledgeQuery

from .embeddings import EmbeddingCallError, EmbeddingError, read_embedding_capture
from .provider_admission import CapturePreflightError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class EmbeddingJobError(EmbeddingError):
    def __init__(self, code, retry_at=None):
        self.code, self.retry_at = code, retry_at
        super().__init__(code)


class EmbeddingPreflightError(EmbeddingJobError):
    pass


RECOVERABLE_PREFLIGHT = frozenset({'EMBEDDING_BUDGET_INSUFFICIENT', 'EMBEDDING_LEDGER_BLOCKED'})


def batch_size(provider):
    return getattr(provider, 'max_batch_size', 1)


def classify_provider_failure(row, provider, error):
    preflight = getattr(provider, 'embedding_preflight', None)
    if isinstance(error, CapturePreflightError):
        reason = error.reason
    elif isinstance(error, EmbeddingCallError) and error.preflight_code in {
            'BUDGET_INSUFFICIENT', 'UNRESOLVED_PHYSICAL_REQUEST', 'BUDGET_PRODUCER_BUSY', 'BUDGET_PRODUCER_SEALED'}:
        reason = error.preflight_code
    elif preflight is not None:
        reason = preflight(row.get('request_id'), error)
    elif isinstance(error, EmbeddingCallError):
        reason = error.preflight_code
    else:
        return error
    code = {
        'EMBEDDING_INPUT_TOO_LARGE': 'EMBEDDING_INPUT_TOO_LARGE',
        'BUDGET_INSUFFICIENT': 'EMBEDDING_BUDGET_INSUFFICIENT',
        'UNRESOLVED_PHYSICAL_REQUEST': 'EMBEDDING_LEDGER_BLOCKED',
        'BUDGET_PRODUCER_BUSY': 'EMBEDDING_LEDGER_BLOCKED',
        'BUDGET_PRODUCER_SEALED': 'EMBEDDING_LEDGER_BLOCKED',
    }.get(reason)
    return EmbeddingPreflightError(code) if code is not None else error


def validate_vectors(vectors, count, dimension):
    if (not isinstance(vectors, list) or len(vectors) != count
            or any(not isinstance(v, list) or len(v) != dimension for v in vectors)
            or any(type(x) not in (int, float) or not math.isfinite(x)
                   for v in vectors for x in v)
            or any(not any(x != 0 for x in v) for v in vectors)):
        raise EmbeddingJobError('EMBEDDING_INVALID_VECTORS')
    return vectors


def retry_delay(exc, attempt):

    if getattr(exc, 'status', None) not in (429, 500, 502, 503, 504):
        return None
    delay = getattr(exc, 'retry_after_seconds', 0) or 0
    if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
        delay = 0
    return max(float(delay), min(60, 2 ** attempt))


class EmbeddingJobs:
    def __init__(self, repository, *, lease_seconds=300, max_attempts=3,
                 circuit_threshold=3, circuit_seconds=60):
        if min(lease_seconds, max_attempts, circuit_threshold, circuit_seconds) < 1:
            raise ValueError('positive job limits required')
        self.repository = repository
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.circuit_threshold, self.circuit_seconds = circuit_threshold, circuit_seconds

    def _tx(self, tenant_id, kb_id):
        return self.repository.job_transaction(tenant_id, kb_id)

    @staticmethod
    def _auth(authorize):
        if not callable(authorize) or authorize() is not True:
            raise EmbeddingJobError('EMBEDDING_AUTHORIZATION_FAILED')

    def _prepare(self, tenant_id, kb_id, build_id, provider, units, audit_context):
        manifest = deepcopy(provider.manifest)
        scope = (tenant_id, kb_id, build_id)
        with self._tx(tenant_id, kb_id) as conn:
            existing = conn.execute(KnowledgeQuery.JOBS_INPUTS, scope).fetchall()
            size = existing[0]['payload']['batch_size'] if existing else batch_size(provider)
            if type(size) is not int or size < 1:
                raise EmbeddingJobError('EMBEDDING_INVALID_BATCH_SIZE')
            payload = {'units': deepcopy(units), 'manifest': manifest,
                       'audit_context': deepcopy(audit_context), 'batch_size': size}
            dependency, provider_hash = digest(payload), digest(manifest)
            if any(row['dependency_hash'] != dependency for row in existing):
                raise EmbeddingJobError('EMBEDDING_DEPENDENCY_CHANGED')
            for number, start in enumerate(range(0, len(units), size)):
                batch = {**payload, 'units': units[start:start + size]}
                conn.execute(KnowledgeQuery.JOB_INSERT,
                             (*scope, number, dependency, provider_hash, batch))
            current = conn.execute(KnowledgeQuery.JOBS_INPUTS, scope).fetchall()
            if any(row['dependency_hash'] != dependency for row in current):
                raise EmbeddingJobError('EMBEDDING_DEPENDENCY_CHANGED')
            conn.execute(KnowledgeQuery.CIRCUIT_ENSURE, (tenant_id, kb_id, provider_hash))
        return manifest, dependency

    def _claim(self, tenant_id, kb_id, build_id, number, dependency):
        scope = (tenant_id, kb_id, build_id, number)
        failure = None
        with self._tx(tenant_id, kb_id) as conn:
            row = conn.execute(KnowledgeQuery.JOB_LOCK, scope).fetchone()
            row['db_now'] = conn.execute(KnowledgeQuery.JOB_CLOCK).fetchone()['db_now']
            if row['dependency_hash'] != dependency:
                raise EmbeddingJobError('EMBEDDING_DEPENDENCY_CHANGED')
            if row['status'] == 'COMPLETE':
                return row
            if row['status'] == 'RUNNING' and row['lease_until'] <= row['db_now']:
                conn.execute(KnowledgeQuery.JOB_EXPIRE, scope)
                conn.execute(KnowledgeQuery.ATTEMPT_EXPIRE,
                             (tenant_id, kb_id, row['request_id']))
                failure = EmbeddingJobError('EMBEDDING_UNKNOWN')
            elif row['status'] in ('RUNNING', 'UNKNOWN', 'ERROR') and not (
                row['status'] == 'ERROR' and row['error'] in RECOVERABLE_PREFLIGHT
            ):
                code = row['error'] if row['status'] == 'ERROR' and row['error'] in {
                    'EMBEDDING_INPUT_TOO_LARGE', 'EMBEDDING_BUDGET_INSUFFICIENT', 'EMBEDDING_LEDGER_BLOCKED'
                } else 'EMBEDDING_' + row['status']
                failure = EmbeddingJobError(code, row['lease_until'])
            elif row['retry_at'] and row['retry_at'] > row['db_now']:
                failure = EmbeddingJobError('EMBEDDING_RETRY', row['retry_at'])
            else:
                circuit = conn.execute(KnowledgeQuery.CIRCUIT_LOCK,
                    (tenant_id, kb_id, row['provider_hash'])).fetchone()
                if circuit['open_until'] and circuit['open_until'] > row['db_now']:
                    failure = EmbeddingJobError('EMBEDDING_CIRCUIT_OPEN', circuit['open_until'])
                else:
                    request_id = 'emb_' + uuid4().hex
                    old_preflight = row['status'] == 'ERROR' and row['error'] in RECOVERABLE_PREFLIGHT
                    row = conn.execute(KnowledgeQuery.JOB_CLAIM,
                        (old_preflight, request_id, self.lease_seconds, *scope)).fetchone()
                    conn.execute(KnowledgeQuery.ATTEMPT_INSERT,
                        (*scope, request_id))
        if failure:
            raise failure
        return row

    def _finish(self, row, *, vectors=None, error=None, recovered=False):
        scope = (row['tenant_id'], row['kb_id'], row['build_id'], row['batch_no'])
        delay = retry_delay(error, row['attempt']) if error else None
        status = 'COMPLETE'
        diagnostic = error.code if isinstance(error, EmbeddingPreflightError) else type(error).__name__ if error else None
        retry_without_attempt = (getattr(error, 'model_sent', None) is False
            or isinstance(error, EmbeddingPreflightError) and error.code in RECOVERABLE_PREFLIGHT)
        if error:
            status = 'ERROR' if isinstance(error, EmbeddingPreflightError) or getattr(error, 'status', None) is not None else 'UNKNOWN'
            if retry_without_attempt:
                status = 'RETRY'
            if delay is not None and row['attempt'] < self.max_attempts:
                status = 'RETRY'
        with self._tx(*scope[:2]) as conn:
            expected = 'UNKNOWN' if recovered else 'RUNNING'
            alternate = 'UNKNOWN' if retry_without_attempt else expected
            updated = conn.execute(KnowledgeQuery.JOB_FINISH,
                (status, vectors, digest(vectors) if vectors is not None else None, diagnostic,
                 retry_without_attempt, delay,
                 *scope, row['request_id'], expected, alternate, recovered or retry_without_attempt)).fetchone()
            if not updated:
                raise EmbeddingJobError('EMBEDDING_LEASE_LOST')
            conn.execute(KnowledgeQuery.ATTEMPT_FINISH,
                         ('ERROR' if status == 'RETRY' else status,
                          diagnostic, *scope[:2], row['request_id']))
            if error and not isinstance(error, EmbeddingPreflightError) and not retry_without_attempt:
                conn.execute(KnowledgeQuery.CIRCUIT_FAILURE,
                    (self.circuit_threshold, self.circuit_seconds, *scope[:2], row['provider_hash']))
            elif not error:
                conn.execute(KnowledgeQuery.CIRCUIT_RESET, (*scope[:2], row['provider_hash']))
        if error:
            raise EmbeddingJobError(diagnostic if isinstance(error, EmbeddingPreflightError) else 'EMBEDDING_' + status,
                                    updated['retry_at']) from error

    def run(self, tenant_id, kb_id, build_id, provider, units, authorize, audit_context):
        self._auth(authorize)
        manifest, dependency = self._prepare(tenant_id, kb_id, build_id, provider, units, audit_context)
        result = []
        batches = self.status(tenant_id, kb_id, build_id)
        for number in range(len(batches)):
            self._auth(authorize)
            row = self._claim(tenant_id, kb_id, build_id, number, dependency)
            batch = row['payload']['units']
            if row['status'] == 'COMPLETE':
                vectors = validate_vectors(row['vectors'], len(batch), provider.dimension)
                if digest(vectors) != row['vectors_hash']:
                    raise EmbeddingJobError('EMBEDDING_RESULT_CORRUPT')
            else:
                try:
                    if provider.manifest != manifest:
                        raise EmbeddingJobError('EMBEDDING_DEPENDENCY_CHANGED')
                    kwargs = {}
                    if 'request_id' in inspect.signature(provider.documents).parameters:
                        kwargs = dict(request_id=row['request_id'], audit_context=audit_context, authorize=authorize)
                    vectors = provider.documents([unit['text'] for unit in batch], **kwargs)
                    vectors = validate_vectors(vectors, len(batch), provider.dimension)
                    self._auth(authorize)
                except Exception as exc:
                    self._finish(row, error=classify_provider_failure(row, provider, exc))
                self._finish(row, vectors=vectors)
            result.extend({**unit, 'vector': vector} for unit, vector in zip(batch, vectors, strict=True))
        self._auth(authorize)
        return result

    def status(self, tenant_id, kb_id, build_id):
        with self._tx(tenant_id, kb_id) as conn:
            return conn.execute(KnowledgeQuery.JOBS_STATUS,
                (tenant_id, kb_id, build_id)).fetchall()

    def reconcile(self, tenant_id, kb_id, build_id, provider, authorize):

        self._auth(authorize)
        with self._tx(tenant_id, kb_id) as conn:
            conn.execute(KnowledgeQuery.JOBS_EXPIRE,
                (tenant_id, kb_id, build_id))
            rows = conn.execute(KnowledgeQuery.JOBS_UNKNOWN, (tenant_id, kb_id, build_id)).fetchall()
            for row in rows:
                conn.execute(KnowledgeQuery.ATTEMPT_EXPIRE,
                    (tenant_id, kb_id, row['request_id']))
        for row in rows:
            vectors = recovered_vectors(row, provider)
            if vectors is not None:
                self._auth(authorize)
                self._finish(row, vectors=vectors, recovered=True)
            elif recovered_unsent(row, provider):
                self._auth(authorize)
                error = EmbeddingJobError('EMBEDDING_KNOWN_UNSENT')
                error.model_sent = False
                try:
                    self._finish(row, error=error, recovered=True)
                except EmbeddingJobError as outcome:
                    if outcome.code != 'EMBEDDING_RETRY':
                        raise
        return self.status(tenant_id, kb_id, build_id)


def recovered_observation(row, provider):
    try:
        recover = getattr(provider, 'recover_embedding', None)
        if recover is not None:
            observation = recover(row['request_id'])
        elif hasattr(provider, 'audit_dir') and hasattr(provider, 'ledger'):
            observation = read_embedding_capture(provider.audit_dir, provider.ledger, row['request_id'])
        else:
            return None
        if observation is None:
            return None
        record = observation['record']
        payload = row['payload']
        texts = [unit['text'] for unit in payload['units']]
        if (not isinstance(record, dict) or record.get('status') not in {'COMPLETE', 'ERROR'}
                or record.get('request_id') != row['request_id']
                or record.get('physical_request_id') != row['request_id'] or record.get('task') != 'document'
                or record.get('texts') != texts or record.get('provider_manifest') != payload['manifest']
                or provider.manifest != payload['manifest'] or record.get('audit_context') != payload['audit_context']
                or observation['physical_status'] != record['status']
                or observation['physical_usage'] != record.get('usage')):
            return None
        return observation
    except (KeyError, TypeError, ValueError, OSError, EmbeddingError):
        return None


def recovered_unsent(row, provider):
    observation = recovered_observation(row, provider)
    if observation is None:
        return False
    record = observation['record']
    reserved, usage = record.get('reserved'), record.get('usage')
    return (record['status'] == 'ERROR' and type(record.get('physical_embedding_attempts')) is int
            and record['physical_embedding_attempts'] == 0
            and isinstance(reserved, dict) and bool(reserved)
            and isinstance(usage, dict) and set(usage) == set(reserved)
            and all(type(value) is int and value == 0 for value in usage.values()))


def recovered_vectors(row, provider):
    try:
        observation = recovered_observation(row, provider)
        if observation is None or observation['record']['status'] != 'COMPLETE':
            return None
        record = observation['record']
        texts = [unit['text'] for unit in row['payload']['units']]

        from .semantic_units import digest as semantic_digest
        vectors = validate_vectors(record.get('vectors'), len(texts), provider.dimension)
        if record.get('vectors_sha256') != semantic_digest(vectors):
            return None
        return vectors
    except (ValueError, OSError, EmbeddingError):
        return None
