from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from .rag_testing.contracts import digest


class CaptureAdmissionError(RuntimeError):
    code = 'RAG_PHYSICAL_REQUEST_ADMISSION_FAILED'


class CapturePreflightError(CaptureAdmissionError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _read(path):
    return json.loads(Path(path).read_text('utf-8'))


def _immutable(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())

        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read(path) != value:
                raise ValueError('PROVIDER_ADMISSION_IMMUTABLE_CONFLICT') from None
    finally:
        temporary.unlink(missing_ok=True)


def _physical_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('INVALID_PROVIDER_PHYSICAL_ID')
    return value


class ProviderAdmission:
    def __init__(self, provider, root):
        identity = {'provider_id': provider.provider_id, 'model': provider.model,
                    'dimension': getattr(provider, 'dimension', None),
                    'endpoint_sha256': digest(provider.manifest.get('endpoint', provider.base_url))}
        self.scope_hash = digest(identity)
        self.directory = Path(root) / self.scope_hash
        self.identity = identity
        self._ephemeral = set()
        metadata = self.directory / 'provider.json'
        if metadata.exists():
            if _read(metadata) != {'schema': 'provider-admission/1', 'scope_hash': self.scope_hash, **identity}:
                raise ValueError('PROVIDER_ADMISSION_IDENTITY_MISMATCH')
        else:
            _immutable(metadata, {'schema': 'provider-admission/1', 'scope_hash': self.scope_hash, **identity})

    @classmethod
    def from_directory(cls, directory):
        value = _read(Path(directory) / 'provider.json')
        instance = object.__new__(cls)
        instance.directory, instance.scope_hash = Path(directory), value['scope_hash']
        instance.identity = {key: value[key] for key in ('provider_id', 'model', 'dimension', 'endpoint_sha256')}
        if value['schema'] != 'provider-admission/1' or digest(instance.identity) != instance.scope_hash:
            raise ValueError('PROVIDER_ADMISSION_IDENTITY_MISMATCH')
        instance._ephemeral = set()
        return instance

    def block(self, physical_id, *, payload_sha256, category):
        _physical_id(physical_id)
        self._ephemeral.add(physical_id)
        path = self.directory / 'events' / (physical_id + '.json')
        value = {'schema': 'provider-permission-error/1', 'scope_hash': self.scope_hash,
                 'physical_request_id': physical_id, 'http_status': 403,
                 'payload_sha256': payload_sha256, 'category': category}
        if path.exists():
            if _read(path) != value:
                raise ValueError('PROVIDER_PERMISSION_RECEIPT_CONFLICT')
        else:
            _immutable(path, value)

    def seed_from_capture(self, root):

        for path in sorted(Path(root).glob('*.json')):
            row = _read(path)
            if row.get('http_status') != 403 or row.get('status') != 'ERROR':
                continue
            payload = row.get('payload', {})
            if row.get('provider_scope') != self.scope_hash:
                continue
            if row.get('payload_sha256') != digest(payload):
                raise ValueError('INVALID_PERMISSION_CAPTURE_HASH')
            self.block(row['physical_request_id'], payload_sha256=row['payload_sha256'], category=row['category'])

    def require_available(self):
        try:
            blocked = self.snapshot()['state'] == 'BLOCKED'
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise CaptureAdmissionError('Provider admission state is unavailable; no request was sent') from exc
        if blocked:
            raise CaptureAdmissionError('Provider is blocked after received HTTP 403; operator acknowledgement required; no request was sent')

    def snapshot(self, *, public=False):
        if _read(self.directory / 'provider.json') != {
                'schema': 'provider-admission/1', 'scope_hash': self.scope_hash, **self.identity}:
            raise ValueError('PROVIDER_ADMISSION_IDENTITY_MISMATCH')
        events, acknowledged, files = set(self._ephemeral), set(), {}
        for path in sorted((self.directory / 'events').glob('*.json')):
            row = _read(path)
            if (row.get('schema') != 'provider-permission-error/1' or
                    row.get('scope_hash') != self.scope_hash or row.get('http_status') != 403 or
                    path.stem != _physical_id(row.get('physical_request_id'))):
                raise ValueError('INVALID_PROVIDER_PERMISSION_RECEIPT')
            events.add(row['physical_request_id'])
            files['events/' + path.name] = sha256(path.read_bytes()).hexdigest()
        for path in sorted((self.directory / 'acknowledgements').glob('*.json')):
            row = _read(path)
            if (row.get('scope_hash') != self.scope_hash or
                    row.get('schema') != 'provider-permission-acknowledgement/1' or
                    path.stem != _physical_id(row.get('acknowledgement_id')) or
                    not isinstance(row.get('observed_error_ids'), list) or
                    not row['observed_error_ids'] or
                    row.get('external_repair_asserted_by_operator') is not True or
                    row.get('provider_probe_performed') is not False or
                    row.get('budget_or_error_rows_modified') is not False):
                raise ValueError('INVALID_PROVIDER_ACKNOWLEDGEMENT')
            ids = {_physical_id(value) for value in row['observed_error_ids']}
            if not ids.issubset(events):
                raise ValueError('ACKNOWLEDGEMENT_WITHOUT_PERMISSION_ERROR')
            acknowledged.update(ids)
            files['acknowledgements/' + path.name] = sha256(path.read_bytes()).hexdigest()
        blocked = sorted(events - acknowledged)
        result = {'provider_id': self.identity['provider_id'], 'model': self.identity['model'],
                  'state': 'BLOCKED' if blocked else 'AVAILABLE',
                  'reason': 'RECEIVED_HTTP_403' if blocked else None, 'http_status': 403 if blocked else None,
                  'unacknowledged_errors': len(blocked), 'automatic_probe': False}
        if not public:
            result.update(scope_hash=self.scope_hash, observed_error_ids=sorted(events),
                          unacknowledged_error_ids=blocked, checkpoint=digest(files))
        return result

    def acknowledge(self, *, expected_checkpoint, evidence_reference):
        if not isinstance(evidence_reference, str) or not evidence_reference.strip():
            raise ValueError('EXPLICIT_EXTERNAL_REPAIR_EVIDENCE_REQUIRED')
        current = self.snapshot()
        if current['checkpoint'] != expected_checkpoint:
            raise ValueError('PROVIDER_ADMISSION_CHECKPOINT_CHANGED')
        if not current['unacknowledged_error_ids']:
            raise ValueError('NO_BLOCKED_PROVIDER_TO_ACKNOWLEDGE')
        receipt = {'schema': 'provider-permission-acknowledgement/1', 'scope_hash': self.scope_hash,
                   'acknowledgement_id': uuid4().hex, 'created_at': time.time(),
                   'expected_checkpoint': expected_checkpoint, 'observed_error_ids': current['observed_error_ids'],
                   'evidence_reference': evidence_reference, 'external_repair_asserted_by_operator': True,
                   'provider_probe_performed': False, 'budget_or_error_rows_modified': False}
        _immutable(self.directory / 'acknowledgements' / (receipt['acknowledgement_id'] + '.json'), receipt)
        return receipt
