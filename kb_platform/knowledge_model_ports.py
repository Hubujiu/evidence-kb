import base64
from binascii import Error as Base64Error
from contextlib import suppress
from copy import deepcopy
from hashlib import sha256
from threading import Lock
from time import monotonic
from uuid import uuid4

from .knowledge_contracts import EMPTY, OBJECT, TEXT, domain_call, object_schema
from .ocr_contract import OCR_CONTRACT_VERSION, validate_ocr_response
from .plugin_contracts import ContractError, Method


CHUNK_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
ERROR_FIELDS = {
    'code': TEXT, 'preflight_code': {'type': ['string', 'null']},
    'status': {'type': ['integer', 'null']}, 'retry_after_seconds': {'type': ['number', 'null']},
    'model_sent': {'type': ['boolean', 'null']}, 'physical_request_id': {'type': ['string', 'null']},
    'call_record': {'type': ['object', 'null']},
}
TOKEN = {'upload_id': TEXT}
METHODS = {
    'metadata': Method(EMPTY, OBJECT),
    'embedding': Method(object_schema({'texts': {'type': 'array', 'minItems': 1,
        'items': TEXT}, 'task': {'enum': ['query', 'document']},
        'context': object_schema({'request_id': TEXT, 'audit_context': OBJECT}, [])}),
        {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'number'}}}),
    'recover_embedding': Method(object_schema({'request_id': TEXT}), {}),
    'embedding_preflight': Method(object_schema({'request_id': TEXT,
        'error': object_schema(ERROR_FIELDS)}), {}),
    'ocr_begin': Method(object_schema({'system': TEXT, 'user': TEXT, 'purpose': {'const': 'ocr'},
        'image_size': {'type': 'integer', 'minimum': 1, 'maximum': MAX_IMAGE_BYTES},
        'image_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'contract_version': {'const': OCR_CONTRACT_VERSION}}), object_schema(TOKEN)),
    'ocr_append': Method(object_schema({**TOKEN, 'offset': {'type': 'integer', 'minimum': 0},
        'data_base64': {'type': 'string', 'minLength': 1, 'maxLength': 4 * ((CHUNK_BYTES + 2) // 3)}}),
        object_schema({'offset': {'type': 'integer', 'minimum': 1}})),
    'ocr': Method(object_schema(TOKEN), OBJECT),
    'ocr_cancel': Method(object_schema(TOKEN), object_schema({'cancelled': {'type': 'boolean'}})),
    'ocr_stats': Method(EMPTY, OBJECT),
}


def _error_details(error):
    from .model_errors import ModelPreflightError

    cause = getattr(error, '__cause__', None)
    details = {key: getattr(error, key, getattr(cause, key, None)) for key in ERROR_FIELDS}
    details['code'] = getattr(error, 'code', type(error).__name__)
    if isinstance(error, ModelPreflightError):
        details['preflight_code'] = error.code
    return details


def _embedding_error(details):
    from .embeddings import EmbeddingCallError
    from .model_errors import ModelPreflightError, RemoteModelError

    cause = (ModelPreflightError(details['preflight_code']) if details['preflight_code'] is not None
             else RemoteModelError(details['code'], details['status'],
                                   retry_after_seconds=details['retry_after_seconds']))
    for key in ('model_sent', 'physical_request_id', 'call_record'):
        if details[key] is not None:
            setattr(cause, key, details[key])
    converted = EmbeddingCallError(cause)
    converted.code = details['code']
    if details['call_record'] is not None:
        converted.call_record = details['call_record']
    return converted


def _embedding_call(operation, request):
    try:
        return operation(request)
    except ContractError as error:
        if error.details.get('kind') == 'embedding':
            raise _embedding_error(error.details) from error
        from .session_identity import PlatformError

        raise PlatformError(error.code, str(error), error.status_code) from error


class AuxiliaryModelEndpoint:
    def __init__(self, *, embedding=None, ocr=None, authorization=None):
        self.embedding_provider, self.ocr_provider = embedding, ocr
        self._authorization = authorization
        self._uploads, self._lock = {}, Lock()

    def metadata(self, request):
        def describe(provider, names):
            return None if provider is None else {name: deepcopy(getattr(provider, name)) for name in names}
        ocr = describe(self.ocr_provider, ('provider_id', 'model', 'manifest'))
        if ocr is not None:
            ocr.update(maximum_format_corrections=getattr(self.ocr_provider, 'maximum_format_corrections', 1),
                       statistics=callable(getattr(self.ocr_provider, 'snapshot_stats', None)))
        return {'embedding': describe(self.embedding_provider,
            ('provider_id', 'model', 'dimension', 'max_batch_size', 'manifest')),
            'ocr': ocr}

    @staticmethod
    def _call(action, *, embedding=False):
        from .embeddings import EmbeddingError
        from .model_errors import ModelPreflightError, RemoteModelError

        try:
            return action()
        except (RemoteModelError, EmbeddingError, ModelPreflightError) as error:
            details = {**_error_details(error), 'kind': 'embedding' if embedding else 'model'}
            raise ContractError(details['code'], str(error),
                                status_code=502, details=details) from error

    def embedding(self, request):
        if self.embedding_provider is None:
            raise ContractError('EMBEDDING_NOT_CONFIGURED', 'Embedding is not configured', status_code=409)
        context = dict(request['context'])
        if set(context) - {'request_id', 'audit_context'}:
            raise ContractError('INVALID_EMBEDDING_CONTEXT', 'Unknown embedding context', status_code=422)
        if self._authorization is not None and 'audit_context' not in context:
            raise ContractError('INVALID_EMBEDDING_CONTEXT', 'Embedding requires explicit source authorization',
                                status_code=422)
        if 'audit_context' in context:
            if self._authorization is None:
                raise ContractError('EMBEDDING_AUTHORIZATION_UNAVAILABLE',
                                    'Embedding audit requires an authorization service', status_code=503)
            authorization = {'audit_context': deepcopy(context['audit_context']), 'task': request['task'],
                             'provider_id': self.embedding_provider.provider_id}

            def authorize():
                return domain_call(self._authorization.validate_embedding, authorization)['authorized'] is True

            context['authorize'] = authorize
        if request['task'] == 'query':
            if len(request['texts']) != 1:
                raise ContractError('INVALID_EMBEDDING_QUERY', 'Query requires one text', status_code=422)
            return self._call(lambda: [self.embedding_provider.query(request['texts'][0], **context)], embedding=True)
        return self._call(lambda: self.embedding_provider.documents(request['texts'], **context), embedding=True)

    def recover_embedding(self, request):
        if self.embedding_provider is None:
            return None
        return self._call(lambda: self.embedding_provider.recover_embedding(request['request_id']), embedding=True)

    def embedding_preflight(self, request):
        if self.embedding_provider is None:
            return None
        return self._call(lambda: self.embedding_provider.embedding_preflight(
            request['request_id'], _embedding_error(request['error'])), embedding=True)

    def _expire(self):
        now = monotonic()
        for identity in list(self._uploads):
            if self._uploads[identity]['expires'] < now:
                del self._uploads[identity]

    def _upload(self, request):
        self._expire()
        upload = self._uploads.get(request['upload_id'])
        if upload is None:
            raise ContractError('OCR_TRANSFER_UNAVAILABLE', 'OCR image transfer unavailable', status_code=409)
        return upload

    def ocr_begin(self, request):
        if self.ocr_provider is None:
            raise ContractError('OCR_NOT_CONFIGURED', 'OCR is not configured', status_code=409)
        with self._lock:
            self._expire()
            if len(self._uploads) >= 8:
                raise ContractError('OCR_TRANSFERS_BUSY', 'Concurrent OCR transfer limit reached', status_code=429)
            identity = uuid4().hex
            self._uploads[identity] = {'request': dict(request), 'content': bytearray(),
                                       'expires': monotonic() + 900}
        return {'upload_id': identity}

    def ocr_append(self, request):
        try:
            chunk = base64.b64decode(request['data_base64'], validate=True)
        except (Base64Error, ValueError, TypeError) as error:
            raise ContractError('INVALID_OCR_TRANSFER', 'Invalid base64 image chunk', status_code=422) from error
        with self._lock:
            upload = self._upload(request)
            content = upload['content']
            if (not 0 < len(chunk) <= CHUNK_BYTES or request['offset'] != len(content)
                    or len(content) + len(chunk) > upload['request']['image_size']):
                raise ContractError('INVALID_OCR_TRANSFER', 'OCR image offset or size does not match', status_code=409)
            content.extend(chunk)
            return {'offset': len(content)}

    def ocr(self, request):
        with self._lock:
            upload = self._upload(request)
            content, expected = bytes(upload['content']), upload['request']
            if len(content) != expected['image_size'] or sha256(content).hexdigest() != expected['image_sha256']:
                raise ContractError('INVALID_OCR_TRANSFER', 'OCR image is incomplete or corrupt', status_code=409)
            del self._uploads[request['upload_id']]
        try:
            result = self._call(lambda: self.ocr_provider.complete_json(expected['system'], expected['user'],
                purpose='ocr', images=[content], validator=validate_ocr_response,
                contract_version=OCR_CONTRACT_VERSION))
            validate_ocr_response(result)
        except ValueError as error:
            raise ContractError('INVALID_MODEL_OUTPUT', str(error), status_code=502,
                                details={'kind': 'model'}) from error
        return result

    def ocr_cancel(self, request):
        with self._lock:
            self._expire()
            return {'cancelled': self._uploads.pop(request['upload_id'], None) is not None}

    def ocr_stats(self, request):
        statistics = getattr(self.ocr_provider, 'snapshot_stats', None)
        if statistics is None:
            raise ContractError('OCR_STATISTICS_UNAVAILABLE', 'OCR provider statistics unavailable', status_code=409)
        return statistics()

    def close(self):
        with self._lock:
            self._uploads.clear()


class EmbeddingClient:
    requires_authorization = True

    def __init__(self, dependency, metadata):
        self._dependency = dependency
        for name in ('provider_id', 'model', 'dimension', 'max_batch_size', 'manifest'):
            setattr(self, name, deepcopy(metadata[name]))

    def _invoke(self, texts, task, request_id, audit_context, authorize):
        if authorize is not None and authorize() is not True:
            raise ValueError('EMBEDDING_AUTHORIZATION_FAILED')
        context = {}
        if request_id is not None:
            context['request_id'] = request_id
        if audit_context is not None:
            context['audit_context'] = audit_context
        vectors = _embedding_call(self._dependency.embedding, {'texts': texts, 'task': task, 'context': context})
        if authorize is not None and authorize() is not True:
            raise ValueError('EMBEDDING_AUTHORIZATION_FAILED')
        if len(vectors) != len(texts) or any(len(value) != self.dimension for value in vectors):
            raise ValueError('EMBEDDING_DIMENSION_MISMATCH')
        return vectors

    def documents(self, texts, *, request_id=None, audit_context=None, authorize=None):
        return self._invoke(texts, 'document', request_id, audit_context, authorize)

    def query(self, query, *, request_id=None, audit_context=None, authorize=None):
        return self._invoke([query], 'query', request_id, audit_context, authorize)[0]

    def recover_embedding(self, request_id):
        return _embedding_call(self._dependency.recover_embedding, {'request_id': request_id})

    def embedding_preflight(self, request_id, error):
        return _embedding_call(self._dependency.embedding_preflight,
                               {'request_id': request_id, 'error': _error_details(error)})


class OCRClient:
    supports_contract_validation = True

    def __init__(self, dependency, metadata):
        self._dependency = dependency
        for name in ('provider_id', 'model', 'manifest'):
            setattr(self, name, deepcopy(metadata[name]))
        self.maximum_format_corrections = metadata['maximum_format_corrections']
        if metadata['statistics']:
            self.snapshot_stats = lambda: domain_call(self._dependency.ocr_stats, {})

    def complete_json(self, system, user, *, purpose, images, validator=None, contract_version=None):
        if (purpose != 'ocr' or len(images) != 1 or not isinstance(images[0], bytes)
                or not 0 < len(images[0]) <= MAX_IMAGE_BYTES or contract_version != OCR_CONTRACT_VERSION):
            raise ValueError('INVALID_OCR_REQUEST')
        data = images[0]
        transfer = domain_call(self._dependency.ocr_begin, {'system': system, 'user': user, 'purpose': purpose,
            'image_size': len(data), 'image_sha256': sha256(data).hexdigest(), 'contract_version': contract_version})
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                chunk = data[offset:offset + CHUNK_BYTES]
                result = domain_call(self._dependency.ocr_append, {**transfer, 'offset': offset,
                    'data_base64': base64.b64encode(chunk).decode('ascii')})
                if result['offset'] != offset + len(chunk):
                    raise ValueError('INVALID_OCR_TRANSFER_ACKNOWLEDGEMENT')
            result = domain_call(self._dependency.ocr, transfer)
        except Exception:
            with suppress(Exception):
                domain_call(self._dependency.ocr_cancel, transfer)
            raise
        if validator is not None:
            validator(result)
        return result


class TextClient:
    def __init__(self, structured):
        self.structured = structured
        self.provider_id = structured.provider_id
        self.manifest = deepcopy(structured.manifest)
        self.model = self.manifest.get('model', '')

    def complete_json(self, system, user, *, purpose):
        return self.structured.complete([{'role': 'system', 'content': system},
            {'role': 'user', 'content': user}], role=purpose,
            schema={'type': 'object'}, call_context={}).value
