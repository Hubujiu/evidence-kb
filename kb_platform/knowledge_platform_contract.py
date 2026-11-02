import base64
from binascii import Error as Base64Error
from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
from threading import Lock
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from .database_contracts import DatabaseError
from .embeddings import EmbeddingError
from .knowledge_contracts import (ACTOR, ARRAY, EMPTY, INTEGER, NULL_OBJECT, OBJECT, TEXT,
                                  ObjectClient, ObjectEndpoint, Operation, domain_call,
                                  methods, object_schema)
from .model_errors import RemoteModelError
from .plugin_contracts import ContractError, Method
from .session_identity import Actor, PlatformError
from .storage_sdk import StorageError


CHUNK_BYTES = 1024 * 1024
STRINGS = {'type': 'array', 'items': {'type': 'string'}}
OPTIONAL_STRINGS = {'type': ['array', 'null'], 'items': {'type': 'string'}}
OPTIONAL_TEXT = {'type': ['string', 'null']}


def _operation(fields, optional=None, response=OBJECT):
    return Operation({'actor': ACTOR, **fields, **(optional or {})},
                     ('actor', *fields), response)


OPERATIONS = {
    'create_kb': _operation({'name': TEXT}),
    'list_kbs': _operation({}, response=ARRAY),
    'set_member': _operation({'kb_id': TEXT, 'principal_id': TEXT, 'capabilities': STRINGS}),
    'bind_source': _operation({'kb_id': TEXT, 'source_id': TEXT, 'source_version_id': TEXT}),
    'remove_binding': _operation({'kb_id': TEXT, 'binding_id': TEXT}),
    'set_source_readers': _operation({'source_id': TEXT, 'readers': STRINGS}),
    'set_source_policy': _operation({'source_id': TEXT, 'policy': OBJECT}),
    'stage': _operation({'kb_id': TEXT}, {'channels': OPTIONAL_STRINGS}),
    'build_status': _operation({'kb_id': TEXT, 'build_id': TEXT}),
    'resume_build': _operation({'kb_id': TEXT, 'build_id': TEXT}),
    'publish': _operation({'kb_id': TEXT, 'build_id': TEXT}),
    'search': _operation({'kb_id': TEXT, 'query': TEXT}, {
        'channel': TEXT, 'top_k': INTEGER, 'context_budget': INTEGER, 'purpose': TEXT}),
    'resolve_citation': _operation({'kb_id': TEXT, 'citation_id': TEXT}, {'purpose': TEXT}),
    'table_query': _operation({'kb_id': TEXT, 'citation_id': TEXT}, {
        'operation': TEXT, 'column': OPTIONAL_TEXT, 'filters': {'type': ['array', 'null']}}),
}
TRANSFER = {'actor': ACTOR, 'upload_id': TEXT}
HASH = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
PROVIDER = {'type': ['object', 'null'], 'properties': {
    'provider_id': OPTIONAL_TEXT, 'manifest': OBJECT}, 'required': ['provider_id', 'manifest'],
    'additionalProperties': False}
METHODS = {
    **methods(OPERATIONS),
    'metadata': Method(EMPTY, object_schema({'embedding': PROVIDER, 'ocr': PROVIDER})),
    'ingest_begin': Method(object_schema({
        'actor': ACTOR, 'kb_id': TEXT, 'filename': TEXT, 'readers': OPTIONAL_STRINGS,
        'policy': NULL_OBJECT, 'source_id': OPTIONAL_TEXT,
        'size': {'type': 'integer', 'minimum': 1}, 'sha256': HASH}),
        object_schema({'upload_id': TEXT})),
    'ingest_append': Method(object_schema({**TRANSFER, 'offset': INTEGER,
        'data_base64': {'type': 'string', 'minLength': 1, 'maxLength': 4 * ((CHUNK_BYTES + 2) // 3)}}),
        object_schema({'offset': INTEGER})),
    'ingest_finish': Method(object_schema(TRANSFER), OBJECT),
    'ingest_cancel': Method(object_schema(TRANSFER), object_schema({'cancelled': {'type': 'boolean'}})),
    'read_original_chunk': Method(object_schema({'actor': ACTOR, 'kb_id': TEXT, 'citation_id': TEXT,
        'purpose': TEXT, 'offset': INTEGER}), object_schema({'offset': INTEGER, 'total': INTEGER,
        'sha256': HASH, 'data_base64': {'type': 'string', 'maxLength': 4 * ((CHUNK_BYTES + 2) // 3)}})),
}


def _call(operation):
    try:
        return operation()
    except (DatabaseError, StorageError) as error:
        raise ContractError('AUTHORITY_UNAVAILABLE', 'Authority unavailable', status_code=503) from error
    except EmbeddingError as error:
        raise ContractError('EMBEDDING_FAILED', str(error), status_code=502) from error
    except RemoteModelError as error:
        details = {'kind': 'model', 'status': error.status,
                   'retry_after_seconds': error.retry_after_seconds}
        if hasattr(error, 'call_record'):
            details['call_record'] = error.call_record
        raise ContractError(error.code, str(error), status_code=502, details=details) from error


def _decode(value):
    try:
        data = base64.b64decode(value, validate=True)
    except (Base64Error, ValueError, TypeError) as error:
        raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Invalid base64 content', 422) from error
    if len(data) > CHUNK_BYTES:
        raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Transfer chunk exceeds the byte limit', 422)
    return data


class PlatformEndpoint(ObjectEndpoint):
    def __init__(self, platform):
        super().__init__(platform, OPERATIONS)
        self._uploads, self._lock = {}, Lock()

    def __getattr__(self, name):
        invoke = super().__getattr__(name)
        return lambda request: _call(lambda: invoke(request))

    def metadata(self, request):
        def provider(value):
            return None if value is None else {
                'provider_id': getattr(value, 'provider_id', None), 'manifest': value.manifest}
        return {'embedding': provider(self._implementation.embedding_provider),
                'ocr': provider(self._implementation.ocr_client)}

    def resolve_citation(self, request):
        values = {key: value for key, value in request.items() if key != 'actor'}
        result = _call(lambda: self._implementation.resolve_citation(Actor(**request['actor']), **values))
        return {key: value for key, value in result.items() if key != 'original_path'}

    def _expire(self):
        now = monotonic()
        for identity in list(self._uploads):
            if self._uploads[identity]['expires'] < now:
                del self._uploads[identity]

    def _upload(self, request):
        self._expire()
        upload = self._uploads.get(request['upload_id'])
        if upload is None or upload['request']['actor'] != request['actor']:
            raise PlatformError('PLATFORM_TRANSFER_UNAVAILABLE', 'Upload unavailable or access denied', 403)
        return upload

    def ingest_begin(self, request):
        from .documents import max_upload_bytes

        if request['size'] > max_upload_bytes():
            raise PlatformError('FILE_SIZE', 'Upload exceeds the configured byte limit', 422)
        with self._lock:
            self._expire()
            if len(self._uploads) >= 8:
                raise PlatformError('PLATFORM_TRANSFERS_BUSY', 'Concurrent upload limit reached', 429)
            identity = uuid4().hex
            self._uploads[identity] = {'request': dict(request), 'content': bytearray(),
                                       'expires': monotonic() + 900}
        return {'upload_id': identity}

    def ingest_append(self, request):
        chunk = _decode(request['data_base64'])
        with self._lock:
            upload = self._upload(request)
            content = upload['content']
            if request['offset'] != len(content) or not chunk or len(content) + len(chunk) > upload['request']['size']:
                raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Upload offset or content size does not match', 409)
            content.extend(chunk)
            return {'offset': len(content)}

    def ingest_finish(self, request):
        with self._lock:
            upload = self._upload(request)
            content, expected = bytes(upload['content']), upload['request']
            if len(content) != expected['size'] or sha256(content).hexdigest() != expected['sha256']:
                raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Upload is incomplete or content hash changed', 409)
            del self._uploads[request['upload_id']]
        arguments = {key: expected[key] for key in ('kb_id', 'filename', 'readers', 'policy', 'source_id')}
        return _call(lambda: self._implementation.ingest(Actor(**request['actor']), data=content, **arguments))

    def ingest_cancel(self, request):
        with self._lock:
            self._expire()
            upload = self._uploads.get(request['upload_id'])
            cancelled = upload is not None and upload['request']['actor'] == request['actor']
            if cancelled:
                del self._uploads[request['upload_id']]
        return {'cancelled': cancelled}

    def read_original_chunk(self, request):
        content = _call(lambda: self._implementation.read_original(Actor(**request['actor']),
            request['kb_id'], request['citation_id'], purpose=request['purpose']))
        offset = request['offset']
        if offset > len(content):
            raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Download offset exceeds original size', 409)
        return {'offset': offset, 'total': len(content), 'sha256': sha256(content).hexdigest(),
                'data_base64': base64.b64encode(content[offset:offset + CHUNK_BYTES]).decode('ascii')}

    def close(self):
        with self._lock:
            self._uploads.clear()


class PlatformClient(ObjectClient):
    def __init__(self, dependency):
        super().__init__(dependency, OPERATIONS)
        metadata = domain_call(dependency.metadata, {})
        self.embedding_provider = SimpleNamespace(**metadata['embedding']) if metadata['embedding'] else None
        self.ocr_client = SimpleNamespace(**metadata['ocr']) if metadata['ocr'] else None

    def ingest(self, actor, kb_id, filename, data, readers=None, policy=None, source_id=None):
        if not isinstance(data, bytes) or not data:
            raise PlatformError('FILE_SIZE', 'Upload must contain bytes', 422)
        identity = asdict(actor)
        begin = domain_call(self._dependency.ingest_begin, {'actor': identity, 'kb_id': kb_id,
            'filename': filename, 'readers': readers, 'policy': policy, 'source_id': source_id,
            'size': len(data), 'sha256': sha256(data).hexdigest()})
        transfer = {'actor': identity, 'upload_id': begin['upload_id']}
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                chunk = data[offset:offset + CHUNK_BYTES]
                result = domain_call(self._dependency.ingest_append, {**transfer, 'offset': offset,
                    'data_base64': base64.b64encode(chunk).decode('ascii')})
                if result['offset'] != offset + len(chunk):
                    raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Upload acknowledgement offset changed', 502)
            return domain_call(self._dependency.ingest_finish, transfer)
        except Exception:
            with suppress(Exception):
                domain_call(self._dependency.ingest_cancel, transfer)
            raise

    def read_original(self, actor, kb_id, citation_id, purpose='download'):
        request = {'actor': asdict(actor), 'kb_id': kb_id, 'citation_id': citation_id, 'purpose': purpose}
        content, expected = bytearray(), None
        while True:
            result = domain_call(self._dependency.read_original_chunk, {**request, 'offset': len(content)})
            identity = result['total'], result['sha256']
            if expected is None:
                expected = identity
            if identity != expected or result['offset'] != len(content):
                raise PlatformError('SOURCE_INTEGRITY', 'Original changed during transfer', 503)
            chunk = _decode(result['data_base64'])
            content.extend(chunk)
            if len(content) > expected[0] or (not chunk and len(content) < expected[0]):
                raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Download content size does not match', 502)
            if len(content) == expected[0]:
                if sha256(content).hexdigest() != expected[1]:
                    raise PlatformError('SOURCE_INTEGRITY', 'Original hash changed during transfer', 503)
                return bytes(content)
