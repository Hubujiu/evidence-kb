from dataclasses import dataclass
from hashlib import sha256
import re
from tempfile import SpooledTemporaryFile
from threading import RLock
from time import monotonic
from uuid import uuid4

from .blob_service import verify_content
from .storage_object_transfer import MAX_TRANSFER_BYTES
from .storage_process import decode_value, encode_value
from .storage_sdk import StorageError


CHUNK_BYTES = 64 * 1024
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_HANDLES = 16
HANDLE_SECONDS = 300


@dataclass
class _Transfer:
    stream: object
    mode: str
    size: int
    digest: str
    tenant: str
    touched: float


class ObjectTransferEndpoint:
    def __init__(self, store):
        self._store, self._handles, self._lock, self._closed = store, {}, RLock(), False

    def _discard(self, handle):
        transfer = self._handles.pop(handle, None)
        if transfer is not None:
            transfer.stream.close()
        return transfer is not None

    def _capacity(self, size):
        if len(self._handles) >= MAX_HANDLES or sum(item.size for item in self._handles.values()) + size > MAX_TRANSFER_BYTES:
            raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')

    def _begin(self, mode, size, digest, tenant, data=None):
        if (type(size) is not int or not 0 <= size <= MAX_OBJECT_BYTES
                or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
                or not isinstance(tenant, str) or not 1 <= len(tenant) <= 1024):
            raise StorageError('STORAGE_OBJECT_TRANSFER_INVALID')
        self._capacity(size)
        stream = SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b')
        try:
            if data is not None:
                stream.write(data)
                stream.seek(0)
            handle = uuid4().hex
            self._handles[handle] = _Transfer(stream, mode, size, digest, tenant, monotonic())
        except BaseException:
            stream.close()
            raise
        return {'handle': handle, 'size': size, 'sha256': digest}

    def execute(self, request):
        fields = {'begin_upload': {'tenant_id', 'size', 'sha256'},
                  'begin_download': {'tenant_id', 'locator', 'sha256'},
                  'write': {'handle', 'offset', 'data'}, 'read': {'handle', 'offset'},
                  'finish_upload': {'handle'}, 'cancel': {'handle'}}
        if (not isinstance(request, dict) or not isinstance(request.get('op'), str)
                or request['op'] not in fields or set(request) != fields[request['op']] | {'op'}):
            raise StorageError('STORAGE_OBJECT_TRANSFER_INVALID')
        with self._lock:
            if self._closed:
                raise StorageError('STORAGE_OBJECT_TRANSFER_CLOSED')
            for handle, transfer in tuple(self._handles.items()):
                if monotonic() - transfer.touched >= HANDLE_SECONDS:
                    self._discard(handle)
            op = request['op']
            if op == 'begin_upload':
                return self._begin('upload', request['size'], request['sha256'], request['tenant_id'])
            if op == 'begin_download':
                ref = self._store.reference(request['tenant_id'], request['locator'], sha256=request['sha256'])
                if ref.size > MAX_OBJECT_BYTES:
                    raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')
                self._capacity(ref.size)
                data = self._store.read(request['tenant_id'], request['locator'], sha256=ref.sha256)
                verify_content(ref, data)
                return self._begin('download', ref.size, ref.sha256, request['tenant_id'], data)
            handle = request['handle']
            if not isinstance(handle, str):
                raise StorageError('STORAGE_OBJECT_TRANSFER_INVALID')
            if op == 'cancel':
                return {'closed': self._discard(handle)}
            transfer = self._handles.get(handle)
            if transfer is None:
                raise StorageError('STORAGE_OBJECT_TRANSFER_HANDLE_INVALID')
            transfer.touched = monotonic()
            expected_mode = 'download' if op == 'read' else 'upload'
            if transfer.mode != expected_mode:
                raise StorageError('STORAGE_OBJECT_TRANSFER_MODE_INVALID')
            if op in {'write', 'read'}:
                if type(request['offset']) is not int or request['offset'] != transfer.stream.tell():
                    raise StorageError('STORAGE_OBJECT_TRANSFER_OFFSET_INVALID')
                if op == 'write':
                    encoded = request['data']
                    if (not isinstance(encoded, dict) or encoded.get('type') != 'bytes'
                            or not isinstance(encoded.get('value'), str)
                            or len(encoded['value']) > ((CHUNK_BYTES + 2) // 3) * 4):
                        raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')
                    data = decode_value(request['data'])
                    if (not isinstance(data, bytes) or not 1 <= len(data) <= CHUNK_BYTES
                            or transfer.stream.tell() + len(data) > transfer.size):
                        raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')
                    transfer.stream.write(data)
                    return {'offset': transfer.stream.tell()}
                data = transfer.stream.read(CHUNK_BYTES)
                offset = transfer.stream.tell()
                done = offset == transfer.size
                if done:
                    self._discard(handle)
                return {'data': encode_value(data), 'offset': offset, 'done': done}
            try:
                if transfer.stream.tell() != transfer.size:
                    raise StorageError('STORAGE_OBJECT_TRANSFER_INCOMPLETE')
                transfer.stream.seek(0)
                data = transfer.stream.read()
                if sha256(data).hexdigest() != transfer.digest:
                    raise StorageError('STORAGE_OBJECT_TRANSFER_INTEGRITY_FAILURE')
                ref = self._store.put(transfer.tenant, data)
                verify_content(ref, data)
                return {'reference': encode_value(ref)}
            finally:
                self._discard(handle)

    def close(self):
        with self._lock:
            self._closed = True
            for handle in tuple(self._handles):
                self._discard(handle)


class ObjectTransferClient:
    def __init__(self, call):
        self._call = call

    def _cancel(self, handle):
        try:
            self._call({'op': 'cancel', 'handle': handle})
        except Exception:
            pass

    def put(self, tenant_id, data):
        if not isinstance(data, bytes) or len(data) > MAX_OBJECT_BYTES:
            raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')
        opened = self._call({'op': 'begin_upload', 'tenant_id': tenant_id,
                             'size': len(data), 'sha256': sha256(data).hexdigest()})
        handle = opened['handle']
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                chunk = data[offset:offset + CHUNK_BYTES]
                result = self._call({'op': 'write', 'handle': handle, 'offset': offset, 'data': encode_value(chunk)})
                if result.get('offset') != offset + len(chunk):
                    raise StorageError('STORAGE_OBJECT_TRANSFER_OFFSET_INVALID')
            result = self._call({'op': 'finish_upload', 'handle': handle})
            ref = decode_value(result['reference'])
            verify_content(ref, data)
            return ref
        finally:
            self._cancel(handle)

    def read(self, tenant_id, locator, *, sha256=None):
        opened = self._call({'op': 'begin_download', 'tenant_id': tenant_id,
                             'locator': locator, 'sha256': sha256})
        handle = opened['handle']
        try:
            if type(opened['size']) is not int or not 0 <= opened['size'] <= MAX_OBJECT_BYTES:
                raise StorageError('STORAGE_OBJECT_TRANSFER_LIMIT')
            data = bytearray()
            while True:
                result = self._call({'op': 'read', 'handle': handle, 'offset': len(data)})
                chunk = decode_value(result['data'])
                if (not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES
                        or len(data) + len(chunk) > opened['size']
                        or type(result.get('offset')) is not int or result['offset'] != len(data) + len(chunk)
                        or type(result.get('done')) is not bool or not chunk and not result['done']):
                    raise StorageError('STORAGE_OBJECT_TRANSFER_INVALID')
                data.extend(chunk)
                if result['done']:
                    break
            from .blob_service import BlobRef

            ref = BlobRef.parse(locator)
            restored = bytes(data)
            verify_content(ref, restored)
            if (len(restored) != opened['size'] or ref.sha256 != opened['sha256']
                    or sha256 is not None and ref.sha256 != sha256):
                raise StorageError('STORAGE_OBJECT_TRANSFER_INTEGRITY_FAILURE')
            return restored
        finally:
            self._cancel(handle)
