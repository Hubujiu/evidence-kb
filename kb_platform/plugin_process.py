from concurrent.futures import Future
from dataclasses import dataclass
from hashlib import sha256
from io import BufferedReader
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
from threading import Condition, RLock, Thread, current_thread
from time import monotonic
from uuid import uuid4

from .plugin_host import PluginSpec
from .plugin_native import PluginError


PROTOCOL = 'evidence-plugin-process/1'
MAX_MESSAGE = 4 * 1024 * 1024
CANCEL_GRACE_SECONDS = 0.25
IO_STOP_SECONDS = 1.0
PROCESS_LAUNCHER = ('import importlib.util,sys; '
    'spec=importlib.util.spec_from_file_location("__main__",sys.argv[2],submodule_search_locations=[sys.argv[1]]); '
    'module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)')


def _document(path):
    value = json.loads(path.read_text('utf-8'))
    if (not isinstance(value, dict) or set(value) - {'protocol', 'name', 'version', 'entry', 'services', 'python', 'command', 'requires'}
            or not {'protocol', 'name', 'version', 'services'} <= set(value)
            or value['protocol'] != PROTOCOL or not isinstance(value['name'], str)
            or not value['name'] or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789._-' for c in value['name'])
            or not isinstance(value['version'], str) or not value['version']
            or not isinstance(value['services'], dict)
            or not value['services']):
        raise PluginError('INVALID_PROCESS_MANIFEST')
    if 'command' in value:
        command = value['command']
        if ('entry' in value or 'python' in value or not isinstance(command, list) or not command
                or any(not isinstance(argument, str) or '\0' in argument for argument in command)
                or not command[0].strip()):
            raise PluginError('INVALID_PROCESS_COMMAND')
    else:
        if not isinstance(value.get('entry'), str) or not value['entry'] or '\0' in value['entry']:
            raise PluginError('INVALID_PROCESS_ENTRY')
        if 'python' in value and (not isinstance(value['python'], str) or '\0' in value['python']
                or not Path(value['python']).is_absolute() or not Path(value['python']).is_file()):
            raise PluginError('INVALID_PROCESS_PYTHON')
        entry = (path.parent / value['entry']).resolve()
        if not entry.is_relative_to(path.parent.resolve()) or not entry.is_file():
            raise PluginError('INVALID_PROCESS_ENTRY')
    for service, methods in value['services'].items():
        if (not isinstance(service, str) or not service or not isinstance(methods, list) or not methods
                or any(not isinstance(method, str) or not method.isidentifier() or method.startswith('_')
                    for method in methods)):
            raise PluginError('INVALID_PROCESS_CAPABILITY')
    requires = value.get('requires', [])
    if (not isinstance(requires, list) or any(not isinstance(service, str) or not service for service in requires)
            or len(set(requires)) != len(requires)):
        raise PluginError('INVALID_PROCESS_DEPENDENCY')
    return value


def _inventory(root, *, installing=False):
    contents = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        cache = '__pycache__' in relative.parts or path.suffix in {'.pyc', '.pyo'}
        if cache and installing:
            continue
        if (cache or path.is_symlink() or path.is_junction()
                or not path.resolve().is_relative_to(root)):
            raise PluginError('PROCESS_PACKAGE_CONTENT_CHANGED')
        if path.is_file():
            contents.append((relative.as_posix(), sha256(path.read_bytes()).hexdigest()))
    return contents


def _verify_package(path, contents):
    if _inventory(path) != [tuple(item) for item in contents]:
        raise PluginError('PROCESS_PACKAGE_CONTENT_CHANGED')


class ProcessPackages:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _publish(self, path, value):
        temporary = self.root / f'{uuid4().hex}.tmp'
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if json.loads(temporary.read_text('utf-8')) != value:
                raise PluginError('INVALID_PROCESS_INSTALLATION')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _installation(self, path):
        try:
            record = json.loads(path.read_text('utf-8'))
            if (not isinstance(record, dict)
                    or set(record) != {'manifest', 'source', 'digest', 'package', 'contents'}
                    or not isinstance(record['source'], str)
                    or not isinstance(record['manifest'], dict)
                    or not isinstance(record['contents'], list)
                    or any(not isinstance(item, list) or len(item) != 2
                        or any(not isinstance(value, str) for value in item) for item in record['contents'])
                    or record['digest'] != path.name.removesuffix('.installation.json')
                    or record['digest'] != sha256(json.dumps(record['contents'],
                        separators=(',', ':')).encode()).hexdigest()
                    or record['package'] != str(self.root / f'{record["manifest"]["name"]}-{record["digest"]}')):
                raise PluginError('INVALID_PROCESS_INSTALLATION')
            package = Path(record['package'])
            if _document(package / 'plugin.json') != record['manifest']:
                raise PluginError('INVALID_PROCESS_INSTALLATION')
            _verify_package(package, record['contents'])
            return record
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise PluginError('INVALID_PROCESS_INSTALLATION') from error

    def install(self, source):
        source = Path(source).resolve()
        manifest = _document(source / 'plugin.json')
        contents = _inventory(source, installing=True)
        digest = sha256(json.dumps(contents, separators=(',', ':')).encode()).hexdigest()
        destination = self.root / f'{manifest["name"]}-{digest}'
        if not destination.exists():
            temporary = self.root / uuid4().hex
            shutil.copytree(source, temporary, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
            _verify_package(temporary, contents)
            temporary.rename(destination)
        _verify_package(destination, contents)
        record = {'manifest': manifest, 'source': str(source), 'digest': digest,
                  'package': str(destination), 'contents': [list(item) for item in contents]}
        metadata = self.root / f'{digest}.installation.json'
        if metadata.exists():
            existing = self._installation(metadata)
            if any(existing[key] != record[key] for key in ('manifest', 'digest', 'package', 'contents')):
                raise PluginError('INVALID_PROCESS_INSTALLATION')
        else:
            self._publish(metadata, record)
        active = self.root / f'{manifest["name"]}.active.json'
        self._publish(active, {'digest': digest})
        return record

    def installed(self):
        return [self._installation(path) for path in sorted(self.root.glob('*.installation.json'))]

    def catalogs(self, *, journal):
        active = {json.loads(path.read_text('utf-8'))['digest'] for path in self.root.glob('*.active.json')}
        return tuple(self.catalog(row['package'], journal=journal)
            for row in (self._installation(self.root / f'{digest}.installation.json')
                for digest in sorted(active)))

    def catalog(self, package, *, journal, timeout=10):
        path = Path(package).resolve()
        if not path.is_relative_to(self.root) or path.parent != self.root:
            raise PluginError('PROCESS_PACKAGE_NOT_INSTALLED')
        metadata = self.root / f'{path.name.rsplit("-", 1)[-1]}.installation.json'
        if not metadata.is_file():
            raise PluginError('PROCESS_PACKAGE_CONTENT_CHANGED')
        record = self._installation(metadata)
        if record['package'] != str(path):
            raise PluginError('PROCESS_PACKAGE_CONTENT_CHANGED')
        _verify_package(path, record['contents'])
        manifest = _document(path / 'plugin.json')
        def mount(ctx, config):
            _verify_package(path, record['contents'])
            transport = ProcessTransport(path, journal=journal, timeout=config.get('timeout', timeout),
                identity=dict(ctx.identity))
            transport.on_exit = lambda: ctx.host._instance_exited(ctx.plugin_id,
                ctx.identity['instance'], ctx.identity['generation'])
            transport.invoke_dependency = lambda call, timeout: ctx.host._invoke(dict(ctx.binding(call['service'])),
                call['method'], call['args'], call['kwargs'], timeout=timeout)
            ctx.defer(transport.close)
            transport.start()
            for service, methods in manifest['services'].items():
                ctx.provide(service, ProcessService(transport, service, tuple(methods), ctx))
        return PluginSpec(f'process.{manifest["name"]}', tuple(manifest['services']), tuple(manifest.get('requires', ())), mount,
            version=manifest['version'], concurrency='concurrent', config_schema={
                'type': 'object', 'properties': {'timeout': {'type': 'number', 'exclusiveMinimum': 0,
                    'maximum': 300}}, 'additionalProperties': False})


@dataclass(frozen=True)
class ProcessService:
    transport: object
    service: str
    methods: tuple[str, ...]
    context: object

    def __getattr__(self, method):
        if method not in self.methods:
            raise AttributeError(method)
        def invoke(*args, **kwargs):
            execution = self.context.host.current_call
            if execution is None:
                return self.context.host.call(self.service, method, *args, **kwargs)
            with self.context.lease():
                identity = {key: execution[key] for key in
                    ('runtime', 'host', 'instance', 'generation', 'operation', 'lease', 'deadline')}
                return self.transport.request('invoke', service=self.service,
                    method=method, args=list(args), kwargs=kwargs, execution=identity,
                    cancel_signal=execution['cancel_signal'])
        return invoke


@dataclass
class _Write:
    request: dict
    payload: bytes
    expires: float
    future: Future
    status: str = 'SENT'
    sent: bool = False
    cancelled: bool = False


class ProcessTransport:
    def __init__(self, package, *, journal, timeout, identity):
        if type(timeout) not in {int, float} or not 0 < timeout <= 300:
            raise PluginError('INVALID_PROCESS_TIMEOUT')
        self.package, self.timeout = Path(package).resolve(), timeout
        self.manifest = _document(self.package / 'plugin.json')
        self.identity, self.session = identity, uuid4().hex
        self.journal = Path(journal).resolve()
        self.journal.mkdir(parents=True, exist_ok=True)
        self._lock, self._terminate_lock = RLock(), RLock()
        self._write_condition = Condition(self._lock)
        self._writes, self._supervisor = {}, None
        self._pending, self._process, self._reader = {}, None, None
        self._outgoing, self._writer, self._writer_stopped = Queue(), None, False
        self._received = {}
        self._callbacks = {}
        self._closed, self._draining, self._failure = False, False, None
        self.on_exit = None
        self.invoke_dependency = None

    def _record(self, request, status, **fields):
        with (self.journal / f'{request["id"]}-{status}.json').open('x', encoding='utf-8') as stream:
            json.dump({'protocol': PROTOCOL, 'identity': self.identity, 'session': self.session,
                'request': request, 'status': status, **fields}, stream, ensure_ascii=False, allow_nan=False)

    def start(self):
        with self._lock:
            if self._process is not None:
                raise PluginError('PROCESS_ALREADY_STARTED')
            if 'command' in self.manifest:
                command = list(self.manifest['command'])
                executable = Path(command[0])
                if not executable.is_absolute() and ('/' in command[0] or '\\' in command[0]):
                    command[0] = str((self.package / executable).resolve())
            else:
                command = [self.manifest.get('python', sys.executable), '-B', '-I', '-u',
                    '-c', PROCESS_LAUNCHER, str(self.package), str(self.package / self.manifest['entry'])]
            self._process = subprocess.Popen(command, cwd=self.package,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self._process.stdout = BufferedReader(self._process.stdout)
            self._reader = Thread(target=self._read, daemon=True)
            self._reader.start()
        response = self.request('handshake', capabilities=self.manifest['services'])
        if (not isinstance(response, dict) or response.get('protocol') != PROTOCOL
                or response.get('services') != self.manifest['services']
                or not isinstance(response.get('runtime'), dict)
                or any(not isinstance(response['runtime'].get(field), str)
                    or not response['runtime'][field].strip() for field in ('language', 'version', 'executable'))):
            raise PluginError('PROCESS_HANDSHAKE_MISMATCH')
        self.runtime_info = response['runtime']

    def _enqueue(self, write):
        with self._write_condition:
            if self._writer_stopped:
                raise PluginError('PROCESS_UNAVAILABLE')
            self._writes[write.request['id']] = write
            write.future.add_done_callback(lambda _: self._write_finished(write.request['id']))
            if self._supervisor is None:
                self._supervisor = Thread(target=self._watch_writes,
                    name=f'plugin-write-deadlines-{self.session}', daemon=True)
                self._supervisor.start()
            if self._writer is None:
                self._writer = Thread(target=self._write, name=f'plugin-writer-{self.session}', daemon=True)
                self._writer.start()
            self._outgoing.put(write)
            self._write_condition.notify_all()

    def _write_finished(self, request_id):
        with self._write_condition:
            self._writes.pop(request_id, None)
            self._write_condition.notify_all()

    def _watch_writes(self):
        while True:
            with self._write_condition:
                if self._writer_stopped:
                    return
                writes = [write for write in self._writes.values() if not write.future.done()]
                if not writes:
                    self._write_condition.wait()
                    continue
                remaining = min(write.expires for write in writes) - monotonic()
                if remaining > 0:
                    self._write_condition.wait(remaining)
                    continue
                expired = [write for write in writes if monotonic() >= write.expires and not write.future.done()]
                blocked = any(write.sent for write in expired)
                for write in expired:
                    write.cancelled = True
                    if not write.sent and not write.future.done():
                        write.future.set_exception(PluginError('PROCESS_DEADLINE_BEFORE_SEND'))
                if not blocked:
                    continue
            self._terminate()
            return

    def _write(self):
        while True:
            write = self._outgoing.get()
            if write is None:
                return
            try:
                with self._lock:
                    if write.cancelled or monotonic() >= write.expires or self._writer_stopped:
                        raise PluginError('PROCESS_DEADLINE_BEFORE_SEND')
                    write.sent = True
                self._record(write.request, write.status)
                with self._lock:
                    if write.cancelled or monotonic() >= write.expires:
                        raise PluginError('PROCESS_DEADLINE_BEFORE_SEND')
                payload = write.payload
                while payload:
                    count = self._process.stdin.write(payload)
                    if not count:
                        raise BrokenPipeError('Process stdin accepted no bytes')
                    payload = payload[count:]
                self._process.stdin.flush()
            except BaseException as error:
                if not write.future.done():
                    write.future.set_exception(error)
            else:
                write.future.set_result(None)

    def _stop_writer(self):
        with self._write_condition:
            if not self._writer_stopped:
                self._writer_stopped = True
                self._outgoing.put(None)
                self._write_condition.notify_all()
            writer = self._writer
        if writer is not None and writer is not current_thread():
            writer.join(IO_STOP_SECONDS)
            if writer.is_alive():
                raise PluginError('PROCESS_WRITER_BUSY')

    def _read(self):
        try:
            while True:
                line = self._process.stdout.readline(MAX_MESSAGE + 1)
                if not line:
                    raise PluginError('PROCESS_EXITED_UNKNOWN')
                if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
                    raise PluginError('PROCESS_MESSAGE_LIMIT')
                value = json.loads(line)
                if isinstance(value, dict) and value.get('kind') == 'call':
                    self._queue_callback(value)
                    continue
                if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
                        or not isinstance(value.get('id'), str) or type(value.get('ok')) is not bool):
                    raise PluginError('PROCESS_INVALID_RESPONSE')
                with self._lock:
                    if value['id'] in self._received:
                        if self._received[value['id']] == value:
                            continue
                        raise PluginError('PROCESS_CONFLICTING_RESPONSE')
                    future = self._pending.get(value['id'])
                    self._received[value['id']] = value
                    if len(self._received) > 1024:
                        self._received.pop(next(iter(self._received)))
                if future is None or future.done():
                    raise PluginError('PROCESS_CONFLICTING_RESPONSE')
                future.set_result(value)
        except BaseException as exc:
            with self._lock:
                self._failure = exc
                futures = tuple(self._pending.values())
            for future in futures:
                if not future.done():
                    future.set_exception(exc)
            if not self._draining and self.on_exit is not None:
                Thread(target=self._notify_exit, args=(exc,),
                    name=f'plugin-exit-{self.session}', daemon=True).start()

    def _queue_callback(self, value):
        if (set(value) != {'protocol', 'id', 'kind', 'parent', 'service', 'method', 'args', 'kwargs'}
                or value['protocol'] != PROTOCOL or not isinstance(value['id'], str) or not value['id']
                or not isinstance(value['parent'], str) or not isinstance(value['service'], str)
                or not isinstance(value['method'], str) or not value['method'].isidentifier()
                or value['method'].startswith('_') or not isinstance(value['args'], list)
                or not isinstance(value['kwargs'], dict)):
            raise PluginError('PROCESS_INVALID_CALLBACK')
        with self._lock:
            callbacks = self._callbacks.get(value['parent'])
            future = self._pending.get(value['parent'])
            if callbacks is None or future is None or future.done() or self.invoke_dependency is None:
                raise PluginError('PROCESS_CALLBACK_PARENT_UNAVAILABLE')
            queue, seen = callbacks
            if value['id'] in seen or len(seen) >= 1024:
                raise PluginError('PROCESS_CALLBACK_DUPLICATE_OR_LIMIT')
            seen.add(value['id'])
            queue.put(value)

    def _invoke_callback(self, call, expires, cancel_signal):
        response = {'protocol': PROTOCOL, 'id': call['id'], 'ok': False}
        try:
            remaining = expires - monotonic()
            if remaining <= 0 or cancel_signal is not None and cancel_signal.is_set():
                raise PluginError('PROCESS_CALLBACK_CANCELLED')
            if call['service'] not in self.manifest.get('requires', ()):
                raise PluginError('UNDECLARED_SERVICE_DEPENDENCY')
            response['result'] = self.invoke_dependency(call, remaining)
            response['ok'] = True
            payload = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
            if len(payload) > MAX_MESSAGE:
                raise PluginError('PROCESS_MESSAGE_LIMIT')
        except Exception as error:
            response = {'protocol': PROTOCOL, 'id': call['id'], 'ok': False,
                'error': {'code': getattr(error, 'code', type(error).__name__), 'message': str(error)}}
            payload = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(payload) > MAX_MESSAGE:
            raise PluginError('PROCESS_MESSAGE_LIMIT')
        record = {'id': uuid4().hex, 'kind': 'dependency_response', 'parent': call['parent'],
            'callback': call['id'], 'service': call['service'], 'method': call['method']}
        self._record(record, 'PREPARED')
        write = _Write(record, payload, expires, Future())
        try:
            self._enqueue(write)
            write.future.result(max(0, expires - monotonic()))
        except BaseException as error:
            with self._lock:
                write.cancelled = True
                sent = write.sent
            if sent:
                try:
                    self._record(record, 'UNKNOWN', reason=type(error).__name__)
                except OSError as failure:
                    error.add_note(f'UNKNOWN journal write failed; SENT remains unresolved: {failure}')
            raise
        self._record(record, 'COMPLETE' if response['ok'] else 'ERROR')

    def _notify_exit(self, error):
        try:
            self.on_exit()
        except BaseException as failure:
            error.add_note(f'Runtime exit notification failed: {failure}')

    def request(self, kind, *, _cleanup=False, execution=None, cancel_signal=None, **fields):
        expires = monotonic() + self.timeout
        if execution is not None and execution.get('deadline') is not None:
            expires = min(expires, execution['deadline'] / 1000)
        remaining = expires - monotonic()
        if remaining <= 0:
            raise PluginError('PROCESS_DEADLINE_BEFORE_SEND')
        request = {'protocol': PROTOCOL, 'id': uuid4().hex, 'kind': kind,
            'identity': execution if execution is not None else self.identity,
            'deadline_seconds': remaining, 'cancelled': False, **fields}
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(payload) > MAX_MESSAGE:
            raise PluginError('PROCESS_MESSAGE_LIMIT')
        future = Future()
        write = _Write(request, payload, expires, Future())
        with self._lock:
            if (self._closed or self._draining and not _cleanup or self._failure
                    or self._process is None or self._process.poll() is not None):
                raise PluginError('PROCESS_UNAVAILABLE')
            self._pending[request['id']] = future
            callbacks = Queue()
            if kind == 'invoke':
                self._callbacks[request['id']] = callbacks, set()
        try:
            self._record(request, 'PREPARED')
            if cancel_signal is not None and cancel_signal.is_set():
                raise PluginError('PROCESS_CANCELLED_BEFORE_SEND')
            self._enqueue(write)
            cancel_expires = None
            while True:
                if write.future.done():
                    write.future.result()
                    if future.done():
                        response = future.result()
                        break
                    try:
                        callback = callbacks.get_nowait()
                    except Empty:
                        pass
                    else:
                        self._invoke_callback(callback, expires, cancel_signal)
                now = monotonic()
                if now >= expires or cancel_expires is not None and now >= cancel_expires:
                    raise TimeoutError('Process request absolute deadline elapsed')
                if cancel_expires is None and cancel_signal is not None and cancel_signal.is_set():
                    with self._lock:
                        if not write.sent:
                            write.cancelled = True
                            raise PluginError('PROCESS_CANCELLED_BEFORE_SEND')
                    cancel_expires = min(expires, now + CANCEL_GRACE_SECONDS)
                    notification = {'protocol': PROTOCOL, 'id': uuid4().hex, 'kind': 'cancel',
                        'target': request['id'], 'identity': request['identity'],
                        'deadline_seconds': cancel_expires - now, 'cancelled': True}
                    self._record(notification, 'CANCEL_REQUESTED')
                    control = json.dumps(notification, separators=(',', ':')).encode() + b'\n'
                    self._enqueue(_Write(notification, control, cancel_expires, Future(), status='CANCEL_SENT'))
                try:
                    future.result(min(0.05, max(0.001, min(expires,
                        cancel_expires if cancel_expires is not None else expires) - monotonic())))
                except TimeoutError:
                    continue
        except BaseException as exc:
            with self._lock:
                write.cancelled = True
                sent = write.sent
            if not sent:
                if isinstance(exc, TimeoutError):
                    raise PluginError('PROCESS_DEADLINE_BEFORE_SEND') from exc
                raise
            try:
                self._record(request, 'UNKNOWN', reason=type(exc).__name__)
            except OSError as failure:
                exc.add_note(f'UNKNOWN journal write failed; SENT remains unresolved: {failure}')
            self._terminate()
            raise PluginError('PROCESS_RESULT_UNKNOWN') from exc
        finally:
            with self._lock:
                self._pending.pop(request['id'], None)
                self._callbacks.pop(request['id'], None)
        if not response['ok']:
            self._record(request, 'ERROR', error=response.get('error'))
            raise PluginError('PROCESS_CALL_ERROR', str(response.get('error')))
        self._record(request, 'COMPLETE')
        return response.get('result')

    def _terminate(self):
        with self._terminate_lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=IO_STOP_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=IO_STOP_SECONDS)
            self._stop_writer()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._draining = True
            if self._pending:
                raise PluginError('PROCESS_BUSY')
        if self._process is not None and self._process.poll() is None and not self._failure:
            try:
                self.request('shutdown', _cleanup=True)
            except PluginError:
                self._terminate()
        self._terminate()
        self._stop_writer()
        if self._supervisor is not None and self._supervisor is not current_thread():
            self._supervisor.join(timeout=IO_STOP_SECONDS)
            if self._supervisor.is_alive():
                raise PluginError('PROCESS_WRITE_SUPERVISOR_BUSY')
        if self._reader is not None:
            self._reader.join(timeout=IO_STOP_SECONDS)
            if self._reader.is_alive():
                raise PluginError('PROCESS_READER_BUSY')
        if self._process is not None:
            self._process.stdin.close()
            self._process.stdout.close()
        with self._lock:
            self._closed = True
