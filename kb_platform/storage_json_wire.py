import base64
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from tempfile import SpooledTemporaryFile
from threading import RLock
from time import monotonic
from uuid import uuid4

from .database_contracts import DatabaseError


MAX_MESSAGE_BYTES = 4 * 1024 * 1024 - 4096
MAX_VALUE_BYTES = 64 * 1024 * 1024
MAX_TRANSFER_BYTES = 256 * 1024 * 1024
MAX_TRANSFERS = 16
CHUNK_BYTES = 256 * 1024
TRANSFER_SECONDS = 300


def encoded(value, *, maximum=MAX_MESSAGE_BYTES):
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 48 or nodes > 1000000:
            raise DatabaseError('STORAGE_WIRE_STRUCTURE_LIMIT')
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED')
            for child in item.values():
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED')

    visit(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED') from None
    if len(payload) > maximum:
        raise DatabaseError('STORAGE_WIRE_MESSAGE_LIMIT')
    return payload


def wire(value, *, maximum=MAX_MESSAGE_BYTES):
    return json.loads(encoded(value, maximum=maximum))


def _fields(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID')


def _identity(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID')
    return value


def _metadata(value):
    _fields(value, {'handle', 'bytes', 'sha256'})
    _identity(value['handle'])
    if (type(value['bytes']) is not int or not 0 < value['bytes'] <= MAX_VALUE_BYTES
            or type(value['sha256']) is not str or re.fullmatch('[0-9a-f]{64}', value['sha256']) is None):
        raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID')
    return value


def _decode(value):
    if type(value) is not str or len(value) > 4 * ((CHUNK_BYTES + 2) // 3):
        raise DatabaseError('STORAGE_WIRE_TRANSFER_LIMIT')
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID') from None
    if not 0 < len(data) <= CHUNK_BYTES:
        raise DatabaseError('STORAGE_WIRE_TRANSFER_LIMIT')
    return data


def _operations(values):
    selected = frozenset(values)
    if not selected or any(type(value) is not str or not value.isidentifier() or value.startswith('wire_')
                           for value in selected):
        raise ValueError('INVALID_STORAGE_WIRE_OPERATIONS')
    return selected


@dataclass
class _Transfer:
    stream: object
    mode: str
    size: int
    digest: str
    transaction: str | None
    touched: float


class StorageJsonEndpoint:
    def __init__(self, handler, validate_transaction=None, *, operations=('execute', 'insert')):
        self._handler, self._validate_transaction = handler, validate_transaction
        self._operations = _operations(operations)
        self._lock, self._transfers, self._closed = RLock(), {}, False

    def _discard(self, handle):
        transfer = self._transfers.pop(handle, None)
        if transfer is not None:
            transfer.stream.close()
        return transfer is not None

    def _expire(self):
        for handle, transfer in tuple(self._transfers.items()):
            if transfer.mode != 'dispatch' and monotonic() - transfer.touched >= TRANSFER_SECONDS:
                self._discard(handle)

    def _new(self, mode, size, digest, transaction, payload=None):
        if self._closed:
            raise DatabaseError('STORAGE_WIRE_CLOSED')
        self._expire()
        if (len(self._transfers) >= MAX_TRANSFERS
                or sum(item.size for item in self._transfers.values()) + size > MAX_TRANSFER_BYTES):
            raise DatabaseError('STORAGE_WIRE_TRANSFER_LIMIT')
        handle = uuid4().hex
        stream = SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b')
        try:
            if payload is not None:
                stream.write(payload)
                stream.seek(0)
            self._transfers[handle] = _Transfer(stream, mode, size, digest, transaction, monotonic())
        except BaseException:
            stream.close()
            raise
        return {'handle': handle, 'bytes': size, 'sha256': digest}

    def result(self, value, *, transaction=None):
        payload = encoded(value, maximum=MAX_VALUE_BYTES)
        if len(payload) <= MAX_MESSAGE_BYTES:
            return json.loads(payload)
        with self._lock:
            return {'wire_result': self._new('download', len(payload), sha256(payload).hexdigest(),
                                             transaction, payload)}

    def execute(self, request):
        request = wire(request)
        if type(request) is not dict or type(request.get('version')) is not int or request['version'] != 1:
            raise DatabaseError('STORAGE_WIRE_VERSION_INVALID')
        operation = request.get('op')
        if type(operation) is not str or not operation.startswith('wire_'):
            with self._lock:
                if self._closed:
                    raise DatabaseError('STORAGE_WIRE_CLOSED')
                self._expire()
            return self.result(self._handler(request))
        with self._lock:
            if self._closed:
                raise DatabaseError('STORAGE_WIRE_CLOSED')
            self._expire()
            if operation == 'wire_begin':
                _fields(request, {'version', 'op', 'bytes', 'sha256', 'transaction'})
                _metadata({'handle': '0' * 32, 'bytes': request['bytes'], 'sha256': request['sha256']})
                if self._validate_transaction is None:
                    if request['transaction'] is not None:
                        raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID')
                else:
                    self._validate_transaction(request['transaction'])
                return self._new('upload', request['bytes'], request['sha256'], request['transaction'])
            fields = {'wire_write': {'offset', 'data_base64'}, 'wire_read': {'offset'},
                      'wire_dispatch': set(), 'wire_cancel': set()}
            if operation not in fields:
                raise DatabaseError('STORAGE_WIRE_OPERATION_UNSUPPORTED')
            _fields(request, {'version', 'op', 'handle'} | fields[operation])
            handle = _identity(request['handle'])
            transfer = self._transfers.get(handle)
            if operation == 'wire_cancel':
                if transfer is not None and transfer.mode == 'dispatch':
                    raise DatabaseError('STORAGE_WIRE_TRANSFER_BUSY')
                return {'closed': self._discard(handle)}
            if transfer is None:
                raise DatabaseError('STORAGE_WIRE_TRANSFER_CLOSED')
            mode = 'download' if operation == 'wire_read' else 'upload'
            if transfer.mode != mode:
                raise DatabaseError('STORAGE_WIRE_TRANSFER_MODE_INVALID')
            transfer.touched = monotonic()
            if operation in {'wire_write', 'wire_read'}:
                if type(request['offset']) is not int or request['offset'] != transfer.stream.tell():
                    raise DatabaseError('STORAGE_WIRE_TRANSFER_OFFSET_INVALID')
                if operation == 'wire_write':
                    data = _decode(request['data_base64'])
                    if transfer.stream.tell() + len(data) > transfer.size:
                        raise DatabaseError('STORAGE_WIRE_TRANSFER_LIMIT')
                    transfer.stream.write(data)
                    return {'offset': transfer.stream.tell()}
                data = transfer.stream.read(CHUNK_BYTES)
                offset = transfer.stream.tell()
                done = offset == transfer.size
                if done:
                    self._discard(handle)
                return {'data_base64': base64.b64encode(data).decode('ascii'), 'offset': offset, 'done': done}
            if transfer.stream.tell() != transfer.size:
                self._discard(handle)
                raise DatabaseError('STORAGE_WIRE_TRANSFER_INCOMPLETE')
            transfer.mode = 'dispatch'
            transfer.stream.seek(0)
            payload = transfer.stream.read()
        try:
            if sha256(payload).hexdigest() != transfer.digest:
                raise DatabaseError('STORAGE_WIRE_TRANSFER_INTEGRITY_FAILURE')
            try:
                logical = json.loads(payload)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED') from None
            logical = wire(logical, maximum=MAX_VALUE_BYTES)
            if (type(logical) is not dict or type(logical.get('op')) is not str
                    or logical['op'] not in self._operations):
                raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID')
            if self._validate_transaction is not None:
                if logical.get('handle') != transfer.transaction:
                    raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID')
                self._validate_transaction(transfer.transaction)
            return self.result(self._handler(logical))
        finally:
            with self._lock:
                self._discard(handle)

    def discard_transaction(self, transaction):
        with self._lock:
            for handle, transfer in tuple(self._transfers.items()):
                if transfer.transaction == transaction and transfer.mode != 'dispatch':
                    self._discard(handle)

    def close(self):
        with self._lock:
            self._closed = True
            for handle, transfer in tuple(self._transfers.items()):
                if transfer.mode != 'dispatch':
                    self._discard(handle)


class StorageJsonClient:
    def __init__(self, send, *, operations=('execute', 'insert')):
        self._send = send
        self._operations = _operations(operations)

    def _frame(self, request):
        return wire(self._send(wire({'version': 1, **request})))

    def _cancel(self, handle):
        with suppress(Exception):
            self._frame({'op': 'wire_cancel', 'handle': handle})

    def _receive(self, result):
        if type(result) is not dict or 'wire_result' not in result:
            return result
        _fields(result, {'wire_result'})
        selected = _metadata(result['wire_result'])
        handle = selected['handle']
        try:
            payload = bytearray()
            while len(payload) < selected['bytes']:
                frame = self._frame({'op': 'wire_read', 'handle': handle, 'offset': len(payload)})
                _fields(frame, {'data_base64', 'offset', 'done'})
                data = _decode(frame['data_base64'])
                if (type(frame['offset']) is not int or frame['offset'] != len(payload) + len(data)
                        or type(frame['done']) is not bool or frame['offset'] > selected['bytes']
                        or frame['done'] != (frame['offset'] == selected['bytes'])):
                    raise DatabaseError('STORAGE_WIRE_TRANSFER_INVALID')
                payload.extend(data)
            if sha256(payload).hexdigest() != selected['sha256']:
                raise DatabaseError('STORAGE_WIRE_TRANSFER_INTEGRITY_FAILURE')
            try:
                decoded = json.loads(payload)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise DatabaseError('STORAGE_WIRE_JSON_REQUIRED') from None
            return wire(decoded, maximum=MAX_VALUE_BYTES)
        finally:
            self._cancel(handle)

    def call(self, request):
        payload = encoded(request, maximum=MAX_VALUE_BYTES)
        if len(payload) <= MAX_MESSAGE_BYTES:
            return self._receive(self._frame(request))
        if request.get('op') not in self._operations:
            raise DatabaseError('STORAGE_WIRE_MESSAGE_LIMIT')
        selected = _metadata(self._frame({'op': 'wire_begin', 'bytes': len(payload),
            'sha256': sha256(payload).hexdigest(), 'transaction': request.get('handle')}))
        handle = selected['handle']
        try:
            if (selected['bytes'] != len(payload) or selected['sha256'] != sha256(payload).hexdigest()):
                raise DatabaseError('STORAGE_WIRE_TRANSFER_INTEGRITY_FAILURE')
            for offset in range(0, len(payload), CHUNK_BYTES):
                chunk = payload[offset:offset + CHUNK_BYTES]
                result = self._frame({'op': 'wire_write', 'handle': handle, 'offset': offset,
                                      'data_base64': base64.b64encode(chunk).decode('ascii')})
                _fields(result, {'offset'})
                if type(result['offset']) is not int or result['offset'] != offset + len(chunk):
                    raise DatabaseError('STORAGE_WIRE_TRANSFER_OFFSET_INVALID')
            result = self._frame({'op': 'wire_dispatch', 'handle': handle})
            return self._receive(result)
        finally:
            self._cancel(handle)
