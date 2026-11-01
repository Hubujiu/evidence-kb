import base64
import json
import math
import re

from .storage_sdk import DOCUMENT_V1, Document, RELATIONAL_V1, StorageError
from .database_contracts import CommitUnknown


MAX_REQUEST_BYTES = 512 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_ITEMS = 10000
MAX_DEPTH = 32
MAX_NODES = 20000


def _size(value, maximum):
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise StorageError('STORAGE_INVALID_PROCESS_VALUE') from None
    if size > maximum:
        raise StorageError('STORAGE_PROCESS_LIMIT')


def encode_value(value, *, _depth=0, _budget=None):
    budget = [MAX_NODES] if _budget is None else _budget
    budget[0] -= 1
    if _depth > MAX_DEPTH or budget[0] < 0:
        raise StorageError('STORAGE_PROCESS_LIMIT')
    if value is None or type(value) in {str, int, bool} or type(value) is float and math.isfinite(value):
        return {'type': 'json', 'value': value}
    if isinstance(value, bytes):
        if len(value) > MAX_RESULT_BYTES:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return {'type': 'bytes', 'value': base64.b64encode(value).decode('ascii')}
    if isinstance(value, Document):
        return {'type': 'document', 'value': {'id': value.id, 'revision': value.revision,
                                             'value': encode_value(value.value, _depth=_depth + 1, _budget=budget)}}
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return {'type': 'sequence', 'value': [encode_value(item, _depth=_depth + 1, _budget=budget)
                                             for item in value]}
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS or any(not isinstance(key, str) for key in value):
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return {'type': 'mapping', 'value': {key: encode_value(item, _depth=_depth + 1, _budget=budget)
                                            for key, item in value.items()}}
    from .blob_service import BlobRef

    if isinstance(value, BlobRef):
        return {'type': 'object_reference', 'value': value.locator}
    raise StorageError('STORAGE_PROCESS_RESULT_UNSUPPORTED')


def decode_value(value, *, _depth=0, _budget=None):
    budget = [MAX_NODES] if _budget is None else _budget
    budget[0] -= 1
    if _depth > MAX_DEPTH or budget[0] < 0:
        raise StorageError('STORAGE_PROCESS_LIMIT')
    if not isinstance(value, dict) or set(value) != {'type', 'value'}:
        raise StorageError('STORAGE_INVALID_PROCESS_VALUE')
    kind, raw = value['type'], value['value']
    if kind == 'json' and (raw is None or type(raw) in {str, int, bool}
                           or type(raw) is float and math.isfinite(raw)):
        return raw
    if kind == 'bytes' and isinstance(raw, str):
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            raise StorageError('STORAGE_INVALID_PROCESS_VALUE') from None
    if kind == 'sequence' and isinstance(raw, list):
        if len(raw) > MAX_ITEMS:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return [decode_value(item, _depth=_depth + 1, _budget=budget) for item in raw]
    if kind == 'mapping' and isinstance(raw, dict) and all(isinstance(key, str) for key in raw):
        if len(raw) > MAX_ITEMS:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return {key: decode_value(item, _depth=_depth + 1, _budget=budget) for key, item in raw.items()}
    if (kind == 'document' and isinstance(raw, dict) and set(raw) == {'id', 'revision', 'value'}
            and isinstance(raw['id'], str) and type(raw['revision']) is int and raw['revision'] > 0):
        document = decode_value(raw['value'], _depth=_depth + 1, _budget=budget)
        if isinstance(document, dict):
            return Document(raw['id'], document, raw['revision'])
    if kind == 'object_reference' and isinstance(raw, str):
        from .blob_service import BlobRef

        try:
            return BlobRef.parse(raw)
        except ValueError:
            raise StorageError('STORAGE_INVALID_PROCESS_VALUE') from None
    raise StorageError('STORAGE_INVALID_PROCESS_VALUE')


def call_json(bound, request):
    _size(request, MAX_REQUEST_BYTES)
    if (not isinstance(request, dict) or set(request) - {'method', 'args', 'kwargs'}
            or not isinstance(request.get('method'), str)
            or not isinstance(request.get('args', []), list)
            or not isinstance(request.get('kwargs', {}), dict)
            or len(request.get('args', [])) > 64 or len(request.get('kwargs', {})) > 64
            or any(not isinstance(key, str) for key in request.get('kwargs', {}))):
        raise StorageError('STORAGE_INVALID_PROCESS_REQUEST')
    method = request['method']
    allowed = bound._methods
    if bound._grant.contract.satisfies(RELATIONAL_V1):
        allowed = allowed & {'query_json', 'batch_json'}
    if method.startswith('_') or method not in allowed:
        raise StorageError('STORAGE_PROCESS_METHOD_REJECTED')
    budget = [MAX_NODES]
    args = [decode_value(value, _budget=budget) for value in request.get('args', [])]
    kwargs = {key: decode_value(value, _budget=budget) for key, value in request.get('kwargs', {}).items()}
    if method == 'put' and bound._grant.contract.satisfies(DOCUMENT_V1):
        document_id = kwargs.get('document_id', args[0] if args else None)
        value = kwargs.get('value', args[1] if len(args) > 1 else None)
        _size(encode_value(Document(document_id, value, 2**63 - 1)), MAX_RESULT_BYTES)
    if method == 'query':
        limit = kwargs.get('limit', args[2] if len(args) > 2 else None)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise StorageError('STORAGE_PROCESS_LIMIT')
    if method in {'stage', 'get_many'} and args:
        values = args[1] if method == 'stage' and len(args) > 1 else args[0]
        if not isinstance(values, list) or len(values) > MAX_ITEMS:
            raise StorageError('STORAGE_PROCESS_LIMIT')
    try:
        result = getattr(bound, method)(*args, **kwargs)
    except StorageError:
        raise
    except CommitUnknown:
        raise
    except Exception as error:
        code = getattr(error, 'code', None)
        if isinstance(code, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,127}', code):
            raise StorageError(code) from None
        raise StorageError('STORAGE_BACKEND_UNAVAILABLE') from None
    try:
        result = encode_value(result)
        _size(result, MAX_RESULT_BYTES)
    except StorageError:
        if method in {'put', 'set', 'delete', 'put_expiring', 'stage', 'seal', 'batch_json'}:
            raise StorageError('STORAGE_WRITE_RESULT_UNAVAILABLE', method=method, outcome='COMPLETED') from None
        raise
    return result
