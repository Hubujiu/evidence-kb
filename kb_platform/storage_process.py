import base64
import json
import math
import re

from .storage_sdk import Document, StorageError
from .database_contracts import CommitUnknown
from .storage_protocol import validate_values


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


def encode_value(value, *, _depth=0, _budget=None, _max_bytes=MAX_RESULT_BYTES):
    budget = [MAX_NODES] if _budget is None else _budget
    budget[0] -= 1
    if _depth > MAX_DEPTH or budget[0] < 0:
        raise StorageError('STORAGE_PROCESS_LIMIT')
    if value is None or type(value) in {str, int, bool} or type(value) is float and math.isfinite(value):
        return {'type': 'json', 'value': value}
    if isinstance(value, bytes):
        if len(value) > _max_bytes:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return {'type': 'bytes', 'value': base64.b64encode(value).decode('ascii')}
    if isinstance(value, Document):
        return {'type': 'document', 'value': {'id': value.id, 'revision': value.revision,
                                             'value': encode_value(value.value, _depth=_depth + 1, _budget=budget, _max_bytes=_max_bytes)}}
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return {'type': 'sequence', 'value': [encode_value(item, _depth=_depth + 1, _budget=budget, _max_bytes=_max_bytes)
                                             for item in value]}
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS or any(not isinstance(key, str) for key in value):
            raise StorageError('STORAGE_PROCESS_LIMIT')
        return {'type': 'mapping', 'value': {key: encode_value(item, _depth=_depth + 1, _budget=budget, _max_bytes=_max_bytes)
                                            for key, item in value.items()}}
    from .blob_service import BlobRef

    if isinstance(value, BlobRef):
        return {'type': 'object_reference', 'value': value.locator}
    raise StorageError('STORAGE_PROCESS_RESULT_UNSUPPORTED')


def decode_value(value, *, _budget=None, _validated=False):
    budget = [MAX_NODES] if _budget is None else _budget
    if not _validated:
        validate_values([value], max_nodes=budget[0])
    budget[0] -= 1
    kind, raw = value['type'], value['value']
    if kind == 'json':
        return raw
    if kind == 'bytes':
        return base64.b64decode(raw, validate=True)
    if kind == 'sequence':
        return [decode_value(item, _budget=budget, _validated=True) for item in raw]
    if kind == 'mapping':
        return {key: decode_value(item, _budget=budget, _validated=True) for key, item in raw.items()}
    if kind == 'document':
        return Document(raw['id'], decode_value(raw['value'], _budget=budget, _validated=True), raw['revision'])
    if kind == 'object_reference':
        from .blob_service import BlobRef

        return BlobRef.parse(raw)
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
    try:
        result = bound._invoke_values(request['method'], request.get('args', []), request.get('kwargs', {}),
            max_nodes=MAX_NODES, request_bytes=MAX_REQUEST_BYTES, result_bytes=MAX_RESULT_BYTES)
    except StorageError:
        raise
    except CommitUnknown:
        raise
    except Exception as error:
        code = getattr(error, 'code', None)
        if isinstance(code, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,127}', code):
            raise StorageError(code) from None
        raise StorageError('STORAGE_BACKEND_UNAVAILABLE') from None
    return result
