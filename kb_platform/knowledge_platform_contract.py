import base64
from binascii import Error as Base64Error
from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
import json
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
from .platform_transfers import CHUNK_BYTES, MAX_PAYLOAD_BYTES, PlatformSnapshots
from .session_identity import Actor, PlatformError
from .storage_sdk import StorageError


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
SNAPSHOT = object_schema({'transfer_id': TEXT,
    'total': {'type': 'integer', 'minimum': 0, 'maximum': MAX_PAYLOAD_BYTES}, 'sha256': HASH})
SNAPSHOT_REQUEST = {'actor': ACTOR, 'transfer_id': TEXT}
CHUNK_RESPONSE = object_schema({'offset': INTEGER, 'total': INTEGER, 'sha256': HASH,
    'data_base64': {'type': 'string', 'maxLength': 4 * ((CHUNK_BYTES + 2) // 3)}})
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
    'ingest_finish': Method(object_schema(TRANSFER), {'oneOf': [
        object_schema({'result': OBJECT}), object_schema({'transfer': SNAPSHOT})]}),
    'ingest_cancel': Method(object_schema(TRANSFER), object_schema({'cancelled': {'type': 'boolean'}})),
    'ingest_result_chunk': Method(object_schema({**SNAPSHOT_REQUEST, 'offset': INTEGER}), CHUNK_RESPONSE),
    'read_original_begin': Method(object_schema({'actor': ACTOR, 'kb_id': TEXT, 'citation_id': TEXT,
        'purpose': TEXT}), SNAPSHOT),
    'read_original_chunk': Method(object_schema({**SNAPSHOT_REQUEST, 'offset': INTEGER}), CHUNK_RESPONSE),
    'transfer_cancel': Method(object_schema(SNAPSHOT_REQUEST),
                              object_schema({'cancelled': {'type': 'boolean'}})),
}


def _call(operation):
    try:
        return operation()
    except (DatabaseError, StorageError) as error:
        if error.code in {'DATABASE_RESULT_LIMIT', 'DATABASE_QUERY_LIMIT', 'DATABASE_QUERY_TIMEOUT'}:
            raise ContractError(error.code, 'Storage operation exceeded its resource limit', status_code=500) from error
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
        self._transfers = PlatformSnapshots()

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
            identity = self._transfers.reserve(request['actor'], 'ingest')
            del self._uploads[request['upload_id']]
        arguments = {key: expected[key] for key in ('kb_id', 'filename', 'readers', 'policy', 'source_id')}
        try:
            result = _call(lambda: self._implementation.ingest(Actor(**request['actor']), data=content, **arguments))
            context = {'kb_id': expected['kb_id'], 'result': result}
            transfer = self._transfers.publish_json(identity, result, context)
            if transfer is None:
                return {'result': result}
            context['result'] = {key: result[key] for key in ('source_id', 'source_version_id')}
            context['result']['parse_snapshot_id'] = result['document']['parse_snapshot_id']
            self._authorize_ingest(request['actor'], context)
            return {'transfer': transfer}
        except BaseException:
            self._transfers.cancel(request['actor'], identity)
            raise

    def ingest_cancel(self, request):
        with self._lock:
            self._expire()
            upload = self._uploads.get(request['upload_id'])
            cancelled = upload is not None and upload['request']['actor'] == request['actor']
            if cancelled:
                del self._uploads[request['upload_id']]
        return {'cancelled': cancelled}

    def _authorize_ingest(self, actor, context):
        return _call(lambda: self._implementation.authorize_ingest_result(
            Actor(**actor), context['kb_id'], **context['result']))

    @staticmethod
    def _original_identity(target):
        return {key: target[key] for key in ('kb_id', 'citation_id', 'source_id', 'source_version_id',
            'parse_snapshot_id', 'build_id', 'original_sha256')}

    def read_original_begin(self, request):
        identity = self._transfers.reserve(request['actor'], 'original')
        actor = Actor(**request['actor'])
        try:
            content = _call(lambda: self._implementation.read_original(actor,
                request['kb_id'], request['citation_id'], purpose=request['purpose']))
            target = _call(lambda: self._implementation.authorize_original(actor,
                request['kb_id'], request['citation_id'], purpose=request['purpose']))
            context = {'request': {key: request[key] for key in ('kb_id', 'citation_id', 'purpose')},
                'identity': self._original_identity(target), 'processing_policy': target['processing_policy']}
            transfer = self._transfers.publish(identity, [content], context)
            if transfer['sha256'] != target['original_sha256']:
                raise PlatformError('SOURCE_INTEGRITY', 'Original hash changed before transfer', 503)
            return transfer
        except BaseException:
            self._transfers.cancel(request['actor'], identity)
            raise

    def _authorize_original(self, actor, context):
        current = _call(lambda: self._implementation.authorize_original(Actor(**actor),
            **context['request'], processing_policy=context['processing_policy']))
        if self._original_identity(current) != context['identity']:
            raise PlatformError('SOURCE_INTEGRITY', 'Original identity changed during transfer', 503)

    def _read_chunk(self, request, kind, authorize):
        result = self._transfers.read(request['actor'], request['transfer_id'], request['offset'], kind,
                                      lambda context: authorize(request['actor'], context))
        data = result.pop('data')
        return {**result, 'data_base64': base64.b64encode(data).decode('ascii')}

    def ingest_result_chunk(self, request):
        return self._read_chunk(request, 'ingest', self._authorize_ingest)

    def read_original_chunk(self, request):
        return self._read_chunk(request, 'original', self._authorize_original)

    def transfer_cancel(self, request):
        return {'cancelled': self._transfers.cancel(request['actor'], request['transfer_id'])}

    def close(self):
        with self._lock:
            self._uploads.clear()
        self._transfers.close()


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
            result = domain_call(self._dependency.ingest_finish, transfer)
            if 'result' in result:
                return result['result']
            content = self._read_transfer(identity, result['transfer'], self._dependency.ingest_result_chunk)
            return json.loads(content)
        except BaseException:
            with suppress(Exception):
                domain_call(self._dependency.ingest_cancel, transfer)
            raise

    def read_original(self, actor, kb_id, citation_id, purpose='download'):
        request = {'actor': asdict(actor), 'kb_id': kb_id, 'citation_id': citation_id, 'purpose': purpose}
        opened = domain_call(self._dependency.read_original_begin, request)
        return self._read_transfer(request['actor'], opened, self._dependency.read_original_chunk)

    def _read_transfer(self, actor, opened, operation):
        request = {'actor': actor, 'transfer_id': opened['transfer_id']}
        content, expected = bytearray(), (opened['total'], opened['sha256'])
        try:
            if type(expected[0]) is not int or not 0 <= expected[0] <= MAX_PAYLOAD_BYTES:
                raise PlatformError('PLATFORM_TRANSFER_LIMIT', 'Invalid transfer size', 502)
            while True:
                result = domain_call(operation, {**request, 'offset': len(content)})
                if (result['total'], result['sha256']) != expected or result['offset'] != len(content):
                    raise PlatformError('SOURCE_INTEGRITY', 'Transfer identity or offset changed', 503)
                chunk = _decode(result['data_base64'])
                content.extend(chunk)
                if len(content) > expected[0] or (not chunk and len(content) < expected[0]):
                    raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Download content size does not match', 502)
                if len(content) == expected[0]:
                    if sha256(content).hexdigest() != expected[1]:
                        raise PlatformError('SOURCE_INTEGRITY', 'Transfer content hash changed', 503)
                    return bytes(content)
        finally:
            with suppress(Exception):
                domain_call(self._dependency.transfer_cancel, request)
