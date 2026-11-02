from dataclasses import asdict
from functools import lru_cache
import json

from .database_contracts import CommitUnknown, DatabaseError
from .plugin_native import PluginError, native_call
from .storage_sdk import StorageError


PROTOCOL = 'storage-service/1'
NATIVE_BATCH_BYTES = 512 * 1024
NATIVE_BATCH_NODES = 1024
BYTE_CHUNK = 256 * 1024
WRITE_METHODS = frozenset({'put', 'set', 'delete', 'put_expiring', 'stage', 'seal', 'batch_json'})


def _call(command, **arguments):
    try:
        return native_call(command, protocol=PROTOCOL, **arguments)
    except PluginError as error:
        raise StorageError(error.code) from error


def contract_descriptor(contract):
    return {'id': contract.id, 'major': contract.major,
            'extends': [contract_descriptor(parent) for parent in contract.extends]}


@lru_cache(maxsize=256)
def _contract(raw, methods):
    return _call('storage_contract_validate', contract=json.loads(raw), methods=list(methods))


def validate_binding(bound):
    contract = contract_descriptor(bound._grant.contract)
    _contract(json.dumps(contract, sort_keys=True), tuple(sorted(bound._methods)))
    expected = {'space': asdict(bound._grant.space), 'dataset_id':
        getattr(bound._grant.target, 'dataset_id', getattr(bound._grant.target, 'cache_id', None))}
    validate_identity(asdict(bound.identity), expected=expected)


def validate_identity(identity, *, expected=None):
    arguments = {'identity': identity}
    if expected is not None:
        arguments['expected'] = expected
    _call('storage_identity_validate', **arguments)


def validate_invocation(bound, method, args, kwargs):
    _call('storage_invocation_validate', contract=contract_descriptor(bound._grant.contract),
          methods=sorted(bound._methods), method=method, argc=len(args), keywords=list(kwargs))


def _scalar_shape(value):
    if type(value) is str:
        return ''
    if type(value) is int:
        return 0
    return value


def _nodes(values):
    stack = [(item, 0) for item in reversed(values)]
    while stack:
        item, depth = stack.pop()
        if (type(item) is not dict or set(item) != {'type', 'value'}
                or type(item['type']) is not str):
            yield {'depth': depth, 'type': 'invalid', 'value': None}, 1
            continue
        kind, value = item['type'], item['value']
        if kind == 'mapping' and type(value) is dict and all(type(key) is str for key in value):
            raw = len(value)
            stack.extend((child, depth + 1) for child in reversed(tuple(value.values())))
        elif kind == 'sequence' and type(value) is list:
            raw = len(value)
            stack.extend((child, depth + 1) for child in reversed(value))
        elif kind == 'document' and type(value) is dict and set(value) == {'id', 'revision', 'value'}:
            body = value['value']
            revision = value['revision']
            raw = {'id': '' if type(value['id']) is str else value['id'],
                   'revision': 1 if type(revision) is int and revision > 0 else revision,
                   'value_kind': body.get('type') if type(body) is dict else None}
            stack.append((body, depth + 1))
        elif kind in {'mapping', 'sequence', 'document'}:
            kind, raw = 'invalid', None
        elif kind == 'json':
            raw = _scalar_shape(value)
        elif kind == 'bytes' and type(value) is str and len(value) > BYTE_CHUNK:
            for offset in range(0, len(value), BYTE_CHUNK):
                yield {'depth': depth, 'type': 'byte_chunk', 'value': {
                    'data': value[offset:offset + BYTE_CHUNK], 'last': offset + BYTE_CHUNK >= len(value)}}, int(offset == 0)
            continue
        else:
            raw = value
        yield {'depth': depth, 'type': kind, 'value': raw}, 1


def validate_values(values, *, max_nodes=20000):
    batch, size, count = [], 0, 0
    for node, weight in _nodes(values):
        count += weight
        if count > max_nodes:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        if node['type'] not in {'mapping', 'sequence', 'document', 'byte_chunk'} and isinstance(node['value'], (list, dict)):
            node = {'depth': node['depth'], 'type': 'invalid', 'value': None}
        try:
            length = len(json.dumps(node, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise StorageError('STORAGE_INVALID_PROCESS_VALUE') from None
        if length > NATIVE_BATCH_BYTES:
            raise StorageError('STORAGE_PROCESS_LIMIT')
        if batch and (size + length > NATIVE_BATCH_BYTES or len(batch) >= NATIVE_BATCH_NODES):
            _call('storage_value_validate', values=batch)
            batch, size = [], 0
        batch.append(node)
        size += length
    if batch:
        _call('storage_value_validate', values=batch)


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
