"""Bounded runtime recovery contracts, independent of RAG quality scoring.

An HTTP timeout after a request may have been sent is UNKNOWN. It must be
reconciled rather than retried merely because the operation is read-only.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import httpx


class RuntimePolicyError(RuntimeError):
    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Failure:
    category: str
    retryable: bool
    outcome: str


def classify_failure(error=None, *, status=None):
    """Only connection admission failures establish that no request was sent."""
    if getattr(error, 'code', '') in {'PROVIDER_RATE_LIMIT_WAIT_EXCEEDED', 'PROVIDER_RETRY_WAIT_EXCEEDED',
            'PROVIDER_REQUEST_UNRESOLVED', 'PROVIDER_REQUEST_ALREADY_SETTLED', 'PROVIDER_CIRCUIT_OPEN',
            'PROVIDER_DEADLINE_EXCEEDED', 'PROVIDER_REQUEST_LIMIT'}:
        return Failure('admission', False, 'ERROR')
    if getattr(error, 'code', '') == 'RAG_PHYSICAL_REQUEST_ADMISSION_FAILED':
        return Failure('admission', False, 'ERROR')
    if status is None and isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    if status is None and isinstance(getattr(error, '__cause__', None), httpx.HTTPError):
        return classify_failure(error.__cause__)
    status = status if status is not None else getattr(error, 'status', None)
    if status is None:
        status = getattr(error, 'status_code', None)
    if status is not None:
        if status == 429:
            return Failure('rate_limit', True, 'ERROR')
        if status in {500, 502, 503, 504}:
            return Failure('unavailable', True, 'ERROR')
        return Failure('authentication' if status in {401, 403} else 'permanent', False, 'ERROR')
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return Failure('connection_not_sent', True, 'ERROR')
    if isinstance(error, httpx.HTTPError) or getattr(error, 'code', '') == 'PROVIDER_OUTCOME_UNKNOWN':
        return Failure('outcome_unknown', False, 'UNKNOWN')
    return Failure('permanent', False, 'ERROR')


def retry_after_seconds(value, *, wall_clock=time.time):
    if not isinstance(value, str):
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return None
            delay = max(0.0, parsed.timestamp() - wall_clock())
        except (ValueError, TypeError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


class ProviderCircuit:
    """Client/provider scoped breaker with exactly one recovery probe."""
    def __init__(self, failure_threshold=3, cooldown_seconds=30.0, *, clock=time.monotonic):
        if (isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int)
                or failure_threshold < 1 or isinstance(cooldown_seconds, bool)
                or not isinstance(cooldown_seconds, (int, float))
                or not math.isfinite(cooldown_seconds) or cooldown_seconds <= 0):
            raise RuntimePolicyError('INVALID_PROVIDER_CIRCUIT_POLICY')
        self.threshold, self.cooldown, self.clock = failure_threshold, float(cooldown_seconds), clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = None
        self._probe = False

    def acquire(self):
        with self._lock:
            if self._opened_at is None:
                return False
            if self.clock() - self._opened_at < self.cooldown or self._probe:
                raise RuntimePolicyError('PROVIDER_CIRCUIT_OPEN')
            self._probe = True
            return True

    def finish(self, probe, *, failure=False, success=False):
        with self._lock:
            if success:
                self._failures, self._opened_at = 0, None
            elif failure:
                self._failures += 1
                if probe or self._failures >= self.threshold:
                    self._opened_at = self.clock()
            if probe:
                self._probe = False

    def snapshot(self):
        with self._lock:
            state = 'HALF_OPEN' if self._probe else 'OPEN' if self._opened_at is not None else 'CLOSED'
            return {'state': state, 'consecutive_failures': self._failures,
                    'failure_threshold': self.threshold, 'cooldown_seconds': self.cooldown}


class RequestReceipts:
    """Durable per-attempt receipts; these contain hashes, never request bodies.

    STARTED is written before the send. UNKNOWN retains the logical request
    guard across restarts. An explicit reconciliation can release that guard;
    its evidence reference and outcome remain in the receipt.
    """
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _pending(self, logical_id):
        return self.directory / (hashlib.sha256(logical_id.encode()).hexdigest() + '.pending')

    def begin(self, logical_id, request_hash, metadata):
        physical_id = uuid4().hex
        pending = self._pending(logical_id)
        row = {'logical_request_id': logical_id, 'physical_request_id': physical_id,
               'request_hash': request_hash, 'status': 'STARTED', 'started_at': time.time(),
               'attempt': metadata.get('attempt'), 'retry_of': metadata.get('retry_of'),
               'retry_reason': metadata.get('retry_reason')}
        try:
            with pending.open('x', encoding='utf-8') as stream:
                json.dump(row, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise RuntimePolicyError('PROVIDER_REQUEST_UNRESOLVED') from None
        try:
            terminal = pending.with_suffix('.terminal')
            if terminal.exists():
                previous = json.loads(terminal.read_text('utf-8'))
                linked = (metadata.get('retry_of') == previous['physical_request_id']
                          and isinstance(metadata.get('retry_reason'), str) and metadata['retry_reason'])
                if (not linked or previous['status'] == 'COMPLETE'
                        and metadata.get('retry_reason') != 'format_correction'):
                    raise RuntimePolicyError('PROVIDER_REQUEST_ALREADY_SETTLED')
            self._write(physical_id, row)
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
        return physical_id

    def _write(self, physical_id, row):
        self._write_path(self.directory / (physical_id + '.json'), row)

    def _write_path(self, target, row):
        temporary = target.with_suffix('.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(row, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def finish(self, logical_id, physical_id, status, **metadata):
        if status not in {'COMPLETE', 'ERROR', 'UNKNOWN'}:
            raise ValueError('INVALID_PHYSICAL_REQUEST_STATE')
        target = self.directory / (physical_id + '.json')
        row = json.loads(target.read_text('utf-8'))
        if row['logical_request_id'] != logical_id or row['status'] != 'STARTED':
            raise RuntimePolicyError('INVALID_PHYSICAL_REQUEST_TRANSITION')
        row.update(status=status, completed_at=time.time(), **metadata)
        self._write(physical_id, row)
        if status != 'UNKNOWN':
            self._write_path(self._pending(logical_id).with_suffix('.terminal'), row)
            self._pending(logical_id).unlink(missing_ok=True)

    def reconcile(self, logical_id, physical_id, *, outcome, evidence_reference):
        if outcome not in {'COMPLETE', 'ERROR'} or not isinstance(evidence_reference, str) or not evidence_reference.strip():
            raise ValueError('RECONCILIATION_EVIDENCE_REQUIRED')
        target = self.directory / (physical_id + '.json')
        row = json.loads(target.read_text('utf-8'))
        pending = self._pending(logical_id)
        active = json.loads(pending.read_text('utf-8'))
        if (row['logical_request_id'] != logical_id or row['status'] not in {'STARTED', 'UNKNOWN'}
                or active['physical_request_id'] != physical_id):
            raise RuntimePolicyError('INVALID_REQUEST_RECONCILIATION')
        row.update(status=outcome, reconciled_at=time.time(), reconciliation_evidence=evidence_reference)
        self._write(physical_id, row)
        self._write_path(pending.with_suffix('.terminal'), row)
        pending.unlink()


@dataclass(frozen=True)
class ToolOutcome:
    status: str
    value: object
    attempts: int
    category: str | None = None
    error_code: str | None = None
    reused: bool = False
    elapsed_seconds: float = 0.0

    def to_dict(self):
        return asdict(self)


class ToolExecutor:
    """Process-local idempotent tool receipts; durable turn storage stays owner.

    Only an explicitly read-only tool can retry a confirmed transient failure.
    Empty results are successful observations, and UNKNOWN is never retried.
    Reusing an outcome still requires the caller's current authority checks.
    """
    def __init__(self, *, max_attempts=2, max_wait_seconds=2.0, sleep=time.sleep):
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3
                or isinstance(max_wait_seconds, bool) or not isinstance(max_wait_seconds, (int, float))
                or not math.isfinite(max_wait_seconds) or max_wait_seconds < 0):
            raise ValueError('INVALID_TOOL_EXECUTION_POLICY')
        self.max_attempts, self.max_wait_seconds, self.sleep = max_attempts, max_wait_seconds, sleep
        self._lock, self._receipts = threading.Lock(), {}

    def execute(self, request_id, tool_name, arguments, function, *, read_only=False, is_empty=None,
                deadline_seconds=None):
        if not isinstance(request_id, str) or not request_id or not isinstance(tool_name, str) or not tool_name:
            raise ValueError('INVALID_TOOL_REQUEST')
        if deadline_seconds is not None and (isinstance(deadline_seconds, bool)
                or not isinstance(deadline_seconds, (int, float)) or not math.isfinite(deadline_seconds)):
            raise ValueError('INVALID_TOOL_DEADLINE')
        started = time.monotonic()
        fingerprint = hashlib.sha256(json.dumps([tool_name, arguments], sort_keys=True,
            ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
        with self._lock:
            existing = self._receipts.get(request_id)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise RuntimePolicyError('TOOL_REQUEST_ID_REUSED')
                if existing['outcome'] is None:
                    raise RuntimePolicyError('TOOL_REQUEST_IN_PROGRESS')
                return ToolOutcome(**{**asdict(deepcopy(existing['outcome'])), 'reused': True})
            self._receipts[request_id] = {'fingerprint': fingerprint, 'outcome': None}
        waited = 0.0
        outcome = None
        try:
            for attempt in range(1, self.max_attempts + 1):
                if deadline_seconds is not None and time.monotonic() - started >= deadline_seconds:
                    outcome = ToolOutcome('ERROR', None, attempt - 1, 'deadline_exceeded',
                                          'TOOL_DEADLINE_EXCEEDED', elapsed_seconds=time.monotonic() - started)
                    break
                try:
                    value = function()
                except Exception as exc:
                    failure = classify_failure(exc)
                    code = getattr(exc, 'code', type(exc).__name__)
                    code = code if isinstance(code, str) and code.isidentifier() else 'TOOL_EXECUTION_FAILED'
                    delay = .25 * 2 ** (attempt - 1)
                    cause = exc if isinstance(exc, httpx.HTTPStatusError) else getattr(exc, '__cause__', None)
                    if isinstance(cause, httpx.HTTPStatusError):
                        retry_after = retry_after_seconds(cause.response.headers.get('Retry-After'))
                        delay = max(delay, retry_after or 0)
                    if (read_only and failure.retryable and attempt < self.max_attempts
                            and waited + delay <= self.max_wait_seconds
                            and (deadline_seconds is None or time.monotonic() - started + delay < deadline_seconds)):
                        self.sleep(delay)
                        waited += delay
                        continue
                    outcome = ToolOutcome(failure.outcome, None, attempt, failure.category, code)
                else:
                    empty = is_empty(value) if is_empty is not None else value is None or value == []
                    elapsed = time.monotonic() - started
                    if deadline_seconds is not None and elapsed >= deadline_seconds:
                        outcome = ToolOutcome('ERROR', None, attempt, 'late_result',
                                              'TOOL_DEADLINE_EXCEEDED', elapsed_seconds=elapsed)
                    else:
                        outcome = ToolOutcome('EMPTY' if empty else 'COMPLETE', value, attempt,
                                              elapsed_seconds=elapsed)
                break
            return deepcopy(outcome)
        except BaseException:
            # Cancellation after invocation cannot establish whether a side
            # effect completed. Preserve the receipt rather than enable replay.
            outcome = ToolOutcome('UNKNOWN', None, attempt, 'outcome_unknown', 'TOOL_EXECUTION_INTERRUPTED')
            raise
        finally:
            with self._lock:
                self._receipts[request_id]['outcome'] = deepcopy(outcome)
