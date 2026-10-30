from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import httpx

from .contracts import digest
from kb_platform.agent_context import estimate_messages
from kb_platform.provider_admission import CaptureAdmissionError, ProviderAdmission
from kb_platform.remote import LunaClient


_context = ContextVar('rag_physical_capture', default={})


@contextmanager
def request_context(value):
    token = _context.set(deepcopy(value))
    try:
        yield
    finally:
        _context.reset(token)


class MeteredTransport(httpx.BaseTransport):
    def __init__(self, inner, ledger, root, *, category, admission=None):
        self.inner, self.ledger, self.root, self.category = inner, ledger, Path(root), category
        self.admission = admission
        self.root.mkdir(parents=True, exist_ok=True)

    def handle_request(self, request):
        if self.admission is not None:
            self.admission.require_available()
        try:
            pending = self.ledger.summary()['pending_or_unknown']
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise CaptureAdmissionError('Physical budget authority is unavailable; no request was sent') from exc
        if pending:
            raise CaptureAdmissionError('Inspect the unresolved physical request before continuing')
        payload = json.loads(request.content)
        model = 'messages' in payload
        resources = dict.fromkeys(self.ledger.caps, 0)
        resources['model' if model else 'remote_embedding_calls'] = 1
        resources['input_tokens' if model else 'embedding_input_tokens'] = (
            estimate_messages(payload['messages'])['tokens'] if model else
            len(json.dumps(payload, ensure_ascii=False).encode('utf-8')))
        if model:
            resources['output_tokens'] = payload.get('max_tokens', 4096)
        identity = uuid4().hex
        try:
            self.ledger.reserve(identity, resources, category=self.category)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise CaptureAdmissionError(str(exc)) from exc
        record = {'physical_request_id': identity, 'payload_sha256': digest(payload),
                  'category': self.category, 'payload': payload, 'context': deepcopy(_context.get()),
                  'status': 'STARTED', 'reserved': resources}
        if self.admission is not None:
            record['provider_scope'] = self.admission.scope_hash
        target = self.root / (identity + '.json')

        def save():
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
        save()
        started = time.perf_counter()
        try:
            response = self.inner.handle_request(request)
            if response.status_code == 403:


                record['http_status'] = 403
                if self.admission is not None:
                    try:
                        self.admission.block(identity, payload_sha256=record['payload_sha256'], category=self.category)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        record['provider_admission_persist_error'] = type(exc).__name__
                save()
            try:
                response.read()
            except httpx.HTTPError as exc:
                if response.status_code != 403:
                    raise
                record['response_body_error'] = type(exc).__name__
                try:
                    response.close()
                except Exception as close_error:
                    record['response_close_error'] = type(close_error).__name__
                response = httpx.Response(403, json={'error': {'code': 'FORBIDDEN_BODY_UNAVAILABLE'}})
            try:
                body = response.json()
            except (ValueError, UnicodeDecodeError):
                body = None
            actual = {'model' if model else 'remote_embedding_calls': 1}
            usage = body.get('usage', {}) if model and isinstance(body, dict) else {}
            if model:
                for original, destination in [('prompt_tokens', 'input_tokens'),
                                               ('completion_tokens', 'output_tokens')]:
                    if type(usage.get(original)) is int and usage[original] >= 0:
                        actual[destination] = usage[original]
                actual.update(remote_embedding_calls=0, embedding_input_tokens=0)
            else:
                token_count = body.get('usageMetadata', {}).get('promptTokenCount') if isinstance(body, dict) else None
                if token_count is None and isinstance(body, dict):
                    token_count = body.get('embedding', {}).get('statistics', {}).get('token_count')
                if token_count is None and isinstance(body, dict):
                    predictions = body.get('predictions', [])
                    counts = [row.get('embeddings', {}).get('statistics', {}).get('token_count') for row in predictions]
                    if counts and all(type(count) is int and count >= 0 for count in counts):
                        token_count = sum(counts)
                if type(token_count) is int and token_count >= 0:
                    actual['embedding_input_tokens'] = token_count
                actual.update(model=0, input_tokens=0, output_tokens=0)
            state = 'COMPLETE' if 200 <= response.status_code < 300 else 'ERROR'
            self.ledger.finish(identity, status=state, usage=actual,
                               diagnostic=None if state == 'COMPLETE' else f'HTTP_{response.status_code}')
            record.update(status=state, http_status=response.status_code, response=body, usage=actual)
            return response
        except BaseException as exc:


            if record['status'] == 'STARTED':
                received_403 = record.get('http_status') == 403
                known_not_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                state = 'ERROR' if received_403 or known_not_sent else 'UNKNOWN'
                usage = dict.fromkeys(self.ledger.caps, 0) if known_not_sent else None
                if received_403:
                    usage = ({'model': 1, 'remote_embedding_calls': 0, 'embedding_input_tokens': 0} if model else
                             {'remote_embedding_calls': 1, 'model': 0, 'input_tokens': 0, 'output_tokens': 0})
                if usage is not None:
                    usage['model' if model else 'remote_embedding_calls'] = 1
                self.ledger.finish(identity, status=state, usage=usage,
                                   diagnostic='HTTP_403' if received_403 else type(exc).__name__)
                record.update(status=state, error=type(exc).__name__)
            raise
        finally:
            record['elapsed_seconds'] = time.perf_counter() - started
            save()

    def close(self):
        self.inner.close()


def observe_client(provider, ledger, root, *, category, inner=None, admission_root=None):

    admission = ProviderAdmission(provider, admission_root or provider.cache_dir / 'provider-admission')
    admission.seed_from_capture(root)
    provider.client.close()
    provider.admission = admission
    native_attempt = getattr(provider, '_capture_native_attempt', provider._send_attempt)
    provider._capture_native_attempt = native_attempt

    def observed_attempt(path, payload, headers, key, metadata):
        admission.require_available()
        try:
            return native_attempt(path, payload, headers, key, metadata)
        except CaptureAdmissionError:


            if 'physical_request_id' in metadata:
                with provider._stats_lock:
                    provider._issued_requests -= 1
                metadata['admission_receipt_id'] = metadata.pop('physical_request_id')
                metadata['known_zero_send'] = True
            raise

    provider._send_attempt = observed_attempt
    provider.client = httpx.Client(transport=MeteredTransport(inner or httpx.HTTPTransport(retries=0),
        ledger, root, category=category, admission=admission), timeout=provider.timeout, trust_env=False)
    return provider


class CapturedLuna(LunaClient):
    def _post(self, path, payload, headers, purpose, key, units=1, *, trace=None):
        admission = getattr(self, 'admission', None)
        if admission is not None:
            admission.require_available()
        metadata = {name: value for name, value in (trace or {}).items() if name in {
            'request_id', 'call_id', 'session_id', 'turn_id', 'agent_role', 'agent_id', 'run_id'}}
        with request_context({**_context.get(), **metadata, 'purpose': purpose, 'request_hash': key}):
            return super()._post(path, payload, headers, purpose, key, units, trace=trace)
