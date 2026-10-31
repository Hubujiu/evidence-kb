from concurrent.futures import Future
import json
import os
from threading import Lock


PROTOCOL = 'plugin-host/1'
MAX_FRAME = 4 * 1024 * 1024 + 64 * 1024


class PipeReader:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self._buffer = bytearray()

    def readline(self, limit):
        while True:
            newline = self._buffer.find(b'\n')
            if newline >= 0 or len(self._buffer) >= limit:
                size = min(newline + 1 if newline >= 0 else limit, limit)
                result = bytes(self._buffer[:size])
                del self._buffer[:size]
                return result
            chunk = os.read(self.descriptor, min(65536, limit - len(self._buffer)))
            if not chunk:
                result, self._buffer = bytes(self._buffer), bytearray()
                return result
            self._buffer.extend(chunk)


class HostProtocolError(OSError):
    def __init__(self, code, message=''):
        self.code = code
        super().__init__(message or code)


def encode_frame(frame):
    payload = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > MAX_FRAME:
        raise HostProtocolError('HOST_FRAME_TOO_LARGE')
    return payload + b'\n'


def read_frame(reader):
    payload = reader.readline(MAX_FRAME + 2)
    if not payload:
        raise EOFError('Rust host connection closed')
    if not payload.endswith(b'\n') or len(payload.rstrip(b'\r\n')) > MAX_FRAME:
        raise HostProtocolError('HOST_INVALID_FRAME_SIZE')
    def constant(value):
        raise ValueError(f'Invalid JSON constant: {value}')
    try:
        frame = json.loads(payload, parse_constant=constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HostProtocolError('HOST_INVALID_JSON') from exc
    if (not isinstance(frame, dict) or frame.get('protocol') != PROTOCOL
            or type(frame.get('id')) is not int or not 0 < frame['id'] < 2**64):
        raise HostProtocolError('HOST_INVALID_ENVELOPE')
    if frame.get('kind') == 'request':
        if (set(frame) != {'protocol', 'kind', 'id', 'method', 'params'}
                or not isinstance(frame['method'], str)):
            raise HostProtocolError('HOST_INVALID_REQUEST')
    elif frame.get('kind') == 'response':
        if type(frame.get('ok')) is not bool:
            raise HostProtocolError('HOST_INVALID_RESPONSE')
        field = 'result' if frame['ok'] else 'error'
        if set(frame) != {'protocol', 'kind', 'id', 'ok', field}:
            raise HostProtocolError('HOST_INVALID_RESPONSE')
        if not frame['ok'] and (not isinstance(frame['error'], dict)
                or not isinstance(frame['error'].get('code'), str)
                or not isinstance(frame['error'].get('message'), str)):
            raise HostProtocolError('HOST_INVALID_ERROR')
    else:
        raise HostProtocolError('HOST_INVALID_FRAME_KIND')
    return frame


class HostConnection:
    def __init__(self, writer):
        self.writer = writer
        self._lock, self._write_lock = Lock(), Lock()
        self._pending = {}
        self._next_id = 0
        self._failure = None

    def send(self, frame):
        payload = encode_frame(frame)
        with self._write_lock:
            with self._lock:
                failure = self._failure
            if failure is not None:
                raise HostProtocolError('HOST_CONNECTION_CLOSED', str(failure))
            try:
                self.writer.write(payload)
                self.writer.flush()
            except OSError as exc:
                self.fail(exc)
                raise

    def request(self, method, params):
        with self._lock:
            if self._failure is not None:
                raise HostProtocolError('HOST_CONNECTION_CLOSED', str(self._failure))
            self._next_id += 1
            identity = self._next_id
            future = Future()
            self._pending[identity] = future
        try:
            self.send({'protocol': PROTOCOL, 'kind': 'request', 'id': identity,
                       'method': method, 'params': params})
            return future.result()
        finally:
            with self._lock:
                self._pending.pop(identity, None)

    def receive(self, frame):
        with self._lock:
            future = self._pending.get(frame['id'])
            if future is None or future.done():
                raise HostProtocolError('HOST_UNKNOWN_RESPONSE')
            if frame['ok']:
                future.set_result(frame['result'])
            else:
                future.set_exception(HostProtocolError(frame['error']['code'], frame['error']['message']))

    def reply(self, identity, result=None, error=None):
        frame = {'protocol': PROTOCOL, 'kind': 'response', 'id': identity, 'ok': error is None}
        if error is None:
            frame['result'] = result
        else:
            frame['error'] = {'code': getattr(error, 'code', type(error).__name__), 'message': str(error)}
        self.send(frame)

    def fail(self, error):
        with self._lock:
            if self._failure is None:
                self._failure = error
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(HostProtocolError('HOST_CONNECTION_CLOSED', str(error)))
