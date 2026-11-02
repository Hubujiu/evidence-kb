from dataclasses import dataclass
from hashlib import sha256
import json
from tempfile import SpooledTemporaryFile
from threading import RLock
from time import monotonic
from uuid import uuid4

from .session_identity import PlatformError


CHUNK_BYTES = 1024 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_TRANSFERS = 16
TRANSFER_SECONDS = 300
INLINE_RESULT_BYTES = 3 * 1024 * 1024


@dataclass
class _Snapshot:
    actor: dict
    kind: str
    size: int = MAX_PAYLOAD_BYTES
    stream: object = None
    digest: str = ''
    context: dict | None = None
    expires: float | None = None


class PlatformSnapshots:
    def __init__(self):
        self._handles, self._lock, self._closed = {}, RLock(), False

    def _discard(self, identity):
        snapshot = self._handles.pop(identity, None)
        if snapshot is not None and snapshot.stream is not None:
            snapshot.stream.close()
        return snapshot is not None

    def _expire(self):
        now = monotonic()
        for identity, snapshot in tuple(self._handles.items()):
            if snapshot.expires is not None and snapshot.expires <= now:
                self._discard(identity)

    def reserve(self, actor, kind):
        with self._lock:
            self._expire()
            if self._closed:
                raise PlatformError('PLATFORM_TRANSFER_UNAVAILABLE', 'Transfer service is closed', 503)
            if (len(self._handles) >= MAX_TRANSFERS
                    or sum(item.size for item in self._handles.values()) + MAX_PAYLOAD_BYTES > MAX_TOTAL_BYTES):
                raise PlatformError('PLATFORM_TRANSFERS_BUSY', 'Concurrent transfer limit reached', 429)
            identity = uuid4().hex
            self._handles[identity] = _Snapshot(dict(actor), kind)
            return identity

    def _publish(self, identity, stream, size, digest, context):
        with self._lock:
            snapshot = self._handles.get(identity)
            if snapshot is None or self._closed or snapshot.stream is not None:
                raise PlatformError('PLATFORM_TRANSFER_UNAVAILABLE', 'Transfer reservation is unavailable', 503)
            stream.seek(0)
            snapshot.stream, snapshot.size, snapshot.digest = stream, size, digest
            snapshot.context, snapshot.expires = context, monotonic() + TRANSFER_SECONDS
            return {'transfer_id': identity, 'total': size, 'sha256': digest}

    def publish(self, identity, chunks, context, *, inline=False):
        stream = SpooledTemporaryFile(max_size=CHUNK_BYTES, mode='w+b')
        digest, size = sha256(), 0
        try:
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_PAYLOAD_BYTES:
                    raise PlatformError('PLATFORM_TRANSFER_LIMIT', 'Transfer exceeds the logical byte limit', 422)
                digest.update(chunk)
                stream.write(chunk)
            if inline and size <= INLINE_RESULT_BYTES:
                stream.close()
                with self._lock:
                    self._discard(identity)
                return None
            return self._publish(identity, stream, size, digest.hexdigest(), context)
        except BaseException:
            stream.close()
            with self._lock:
                self._discard(identity)
            raise

    def publish_json(self, identity, value, context):
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
        return self.publish(identity, (part.encode('utf-8') for part in encoder.iterencode(value)),
                            context, inline=True)

    def _get(self, actor, identity, kind):
        self._expire()
        snapshot = self._handles.get(identity)
        if (snapshot is None or snapshot.stream is None or snapshot.actor != actor or snapshot.kind != kind):
            raise PlatformError('PLATFORM_TRANSFER_UNAVAILABLE', 'Transfer unavailable or access denied', 403)
        return snapshot

    def read(self, actor, identity, offset, kind, authorize):
        with self._lock:
            snapshot = self._get(actor, identity, kind)
        try:
            authorize(snapshot.context)
        except BaseException:
            with self._lock:
                self._discard(identity)
            raise
        with self._lock:
            if self._get(actor, identity, kind) is not snapshot:
                raise PlatformError('PLATFORM_TRANSFER_UNAVAILABLE', 'Transfer was replaced', 409)
            if type(offset) is not int or offset != snapshot.stream.tell():
                raise PlatformError('INVALID_PLATFORM_TRANSFER', 'Download offset is not the next chunk', 409)
            data = snapshot.stream.read(CHUNK_BYTES)
            result = {'offset': offset, 'total': snapshot.size, 'sha256': snapshot.digest, 'data': data}
            if snapshot.stream.tell() == snapshot.size:
                self._discard(identity)
            return result

    def cancel(self, actor, identity):
        with self._lock:
            self._expire()
            snapshot = self._handles.get(identity)
            return snapshot is not None and snapshot.actor == actor and self._discard(identity)

    def close(self):
        with self._lock:
            self._closed = True
            for identity in tuple(self._handles):
                self._discard(identity)
