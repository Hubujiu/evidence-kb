from contextlib import contextmanager, suppress
from dataclasses import asdict
from functools import lru_cache
import json

from .database_contracts import CommitUnknown, DatabaseError
from .plugin_native import PluginError, native_call
from .storage_sdk import StorageError


PROTOCOL = 'storage-service/1'
NATIVE_BATCH_BYTES = 512 * 1024
NATIVE_BATCH_EVENTS = 1024
TEXT_CHUNK = 32 * 1024
MAX_VALUE_BYTES = 96 * 1024 * 1024
MAX_VALUE_NODES = 250000


def _call(command, **arguments):
    try:
        return native_call(command, protocol=PROTOCOL, **arguments)
    except PluginError as error:
        raise StorageError(error.code) from error


def _service(action, **arguments):
    response = _call('storage_service', action=action, **arguments)
    if not response['ok']:
        error = response['error']
        raise StorageError(error['code'], **error.get('details', {}))
    return response['result']


def contract_descriptor(contract):
    return {'id': contract.id, 'major': contract.major,
            'extends': [contract_descriptor(parent) for parent in contract.extends]}


@lru_cache(maxsize=256)
def _describe(raw):
    return _service('describe', descriptor=json.loads(raw))


def describe(identity, contract, methods):
    selected = {'identity': asdict(identity), 'contract': contract_descriptor(contract),
                'methods': sorted(methods)}
    return json.loads(json.dumps(_describe(json.dumps(selected, sort_keys=True))))


def validate_descriptor(descriptor):
    selected = {key: descriptor[key] for key in ('identity', 'contract', 'methods')}
    return json.loads(json.dumps(_describe(json.dumps(selected, sort_keys=True))))


def validate_binding(bound):
    expected = {'space': asdict(bound._grant.space), 'dataset_id':
        getattr(bound._grant.target, 'dataset_id', getattr(bound._grant.target, 'cache_id', None))}
    validate_identity(asdict(bound.identity), expected=expected)
    return describe(bound.identity, bound._grant.contract, bound._methods)


def validate_identity(identity, *, expected=None):
    arguments = {'identity': identity}
    if expected is not None:
        arguments['expected'] = expected
    _call('storage_identity_validate', **arguments)


def _text_events(value, kind='string'):
    if type(value) is not str:
        yield {'event': 'invalid'}
        return
    yield {'event': 'open', 'type': kind}
    for offset in range(0, len(value), TEXT_CHUNK):
        yield {'event': 'chunk', 'value': value[offset:offset + TEXT_CHUNK]}
    yield {'event': 'close'}


def _events(item):
    if type(item) is not dict or set(item) != {'type', 'value'} or type(item['type']) is not str:
        yield {'event': 'invalid'}
        return
    kind, value = item['type'], item['value']
    if kind == 'json':
        if type(value) is str:
            yield from _text_events(value)
        elif type(value) is int:
            yield {'event': 'scalar', 'type': 'integer', 'value': str(value)}
        else:
            yield {'event': 'scalar', 'type': 'json', 'value': value}
    elif kind == 'bytes':
        yield from _text_events(value, 'bytes')
    elif kind == 'object_reference':
        yield {'event': 'scalar', 'type': kind, 'value': value}
    elif kind == 'mapping' and type(value) is dict:
        yield {'event': 'open', 'type': kind, 'length': len(value)}
        for key, child in value.items():
            yield from _text_events(key, 'key')
            yield from _events(child)
        yield {'event': 'close'}
    elif kind == 'sequence' and type(value) is list:
        yield {'event': 'open', 'type': kind, 'length': len(value)}
        for child in value:
            yield from _events(child)
        yield {'event': 'close'}
    elif kind == 'document' and type(value) is dict and set(value) == {'id', 'revision', 'value'}:
        revision = value['revision']
        yield {'event': 'open', 'type': kind, 'revision': str(revision) if type(revision) is int else revision}
        yield from _text_events(value['id'])
        yield from _events(value['value'])
        yield {'event': 'close'}
    else:
        yield {'event': 'invalid'}


def _feed(handle, values):
    batch, size = [], 0
    try:
        for item in values:
            for event in _events(item):
                length = len(json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())
                if length > NATIVE_BATCH_BYTES:
                    raise StorageError('STORAGE_PROCESS_LIMIT')
                if batch and (size + length > NATIVE_BATCH_BYTES or len(batch) >= NATIVE_BATCH_EVENTS):
                    _service('feed', handle=handle, events=batch)
                    batch, size = [], 0
                batch.append(event)
                size += length
        if batch:
            _service('feed', handle=handle, events=batch)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise StorageError('STORAGE_INVALID_PROCESS_VALUE') from None


def _limits(max_nodes, max_bytes):
    return {'max_nodes': max_nodes, 'max_bytes': max_bytes}


def validate_values(values, *, max_nodes=20000, max_bytes=MAX_VALUE_BYTES):
    handle = _service('values', limits=_limits(max_nodes, max_bytes))['handle']
    try:
        _feed(handle, values)
        _service('finish', handle=handle)
    finally:
        with suppress(StorageError):
            _service('close', handle=handle)


def result_failure(descriptor, method, error):
    code = getattr(error, 'code', 'STORAGE_PROCESS_RESULT_UNSUPPORTED')
    try:
        _service('result_error', descriptor={key: descriptor[key] for key in ('identity', 'contract', 'methods')},
                 method=method, code=code)
    except StorageError as classified:
        if classified.code == 'STORAGE_WRITE_RESULT_UNAVAILABLE':
            raise classified from error
        if descriptor['operations'][method]['effect'] == 'write':
            raise StorageError('STORAGE_WRITE_RESULT_UNAVAILABLE', method=method, outcome='COMPLETED') from error
        raise classified from error


class PreparedCall:
    def __init__(self, handle, descriptor, method, operation):
        self._handle, self._descriptor, self._method = handle, descriptor, method
        self.operation, self._completed = operation, False

    def complete(self, encode):
        if self._completed:
            raise StorageError('STORAGE_CALL_STATE_INVALID')
        self._completed = True
        try:
            _service('result', handle=self._handle)
            result = encode()
            _feed(self._handle, [result])
            _service('finish', handle=self._handle)
            return result
        except Exception as error:
            if getattr(error, 'code', None) == 'STORAGE_WRITE_RESULT_UNAVAILABLE':
                raise
            result_failure(self._descriptor, self._method, error)


@contextmanager
def prepare(descriptor, method, args, kwargs, *, max_nodes=MAX_VALUE_NODES,
            request_bytes=MAX_VALUE_BYTES, result_bytes=MAX_VALUE_BYTES):
    handle = _service('open', descriptor={key: descriptor[key] for key in ('identity', 'contract', 'methods')},
        method=method, argc=len(args), keywords=list(kwargs),
        limits={'request': _limits(max_nodes, request_bytes), 'response': _limits(max_nodes, result_bytes)})['handle']
    try:
        _feed(handle, [*args, *kwargs.values()])
        operation = _service('admit', handle=handle)['operation']
        yield PreparedCall(handle, descriptor, method, operation)
    finally:
        with suppress(StorageError):
            _service('close', handle=handle)


def validate_structured(request):
    if type(request) is not dict:
        raise DatabaseError('STORAGE_WIRE_VERSION_INVALID')
    header = {'fields': list(request), 'control': {key: value for key, value in request.items()
              if key not in {'statement', 'values', 'updates', 'returning'}}}
    try:
        _call('storage_structured_validate', header=header)
    except StorageError as error:
        raise DatabaseError(error.code) from error


def resolve_commit(operation_id, result):
    response = ({key: result[key] for key in ('status', 'operation_id') if key in result}
                if type(result) is dict else None)
    try:
        receipt = _call('storage_commit_resolve', operation_id=operation_id, response=response)
    except StorageError as error:
        raise CommitUnknown(operation_id) from error
    if receipt['status'] == 'UNKNOWN':
        raise CommitUnknown(operation_id)
    if receipt['status'] == 'ROLLED_BACK':
        raise DatabaseError('STORAGE_TRANSACTION_ROLLED_BACK')
    return receipt
