from concurrent.futures import Future
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from threading import RLock, Thread
from time import monotonic
from uuid import uuid4

from .plugin_host import PluginSpec
from .plugin_native import PluginError, native_call


PROTOCOL = 'evidence-plugin-process/1'
MAX_MESSAGE = 4 * 1024 * 1024
PROCESS_LAUNCHER = ('import importlib.util,sys; '
    'spec=importlib.util.spec_from_file_location("__main__",sys.argv[2],submodule_search_locations=[sys.argv[1]]); '
    'module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)')


def _document(path):
    value = json.loads(path.read_text('utf-8'))
    if (not isinstance(value, dict) or set(value) - {'protocol', 'name', 'version', 'entry', 'services', 'python'}
            or not {'protocol', 'name', 'version', 'entry', 'services'} <= set(value)
            or value['protocol'] != PROTOCOL or not isinstance(value['name'], str)
            or not value['name'] or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789._-' for c in value['name'])
            or not isinstance(value['version'], str) or not value['version']
            or not isinstance(value['entry'], str) or not isinstance(value['services'], dict)
            or not value['services']):
        raise PluginError('INVALID_PROCESS_MANIFEST')
    if 'python' in value and (not isinstance(value['python'], str)
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
    return value


class ProcessPackages:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def install(self, source):
        source = Path(source).resolve()
        manifest = _document(source / 'plugin.json')
        contents = [(path.relative_to(source).as_posix(), sha256(path.read_bytes()).hexdigest())
            for path in sorted(source.rglob('*')) if path.is_file()]
        digest = sha256(json.dumps(contents, separators=(',', ':')).encode()).hexdigest()
        destination = self.root / f'{manifest["name"]}-{digest}'
        if not destination.exists():
            temporary = self.root / uuid4().hex
            shutil.copytree(source, temporary)
            temporary.rename(destination)
        record = {'manifest': manifest, 'source': str(source), 'digest': digest,
                  'package': str(destination), 'contents': contents}
        metadata = self.root / f'{digest}.installation.json'
        if not metadata.exists():
            with metadata.open('x', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=False)
        active = self.root / f'{manifest["name"]}.active.json'
        temporary = self.root / f'{uuid4().hex}.tmp'
        temporary.write_text(json.dumps({'digest': digest}), 'utf-8')
        temporary.replace(active)
        return record

    def installed(self):
        return [json.loads(path.read_text('utf-8')) for path in sorted(self.root.glob('*.installation.json'))]

    def catalogs(self, *, journal):
        active = {json.loads(path.read_text('utf-8'))['digest'] for path in self.root.glob('*.active.json')}
        return tuple(self.catalog(row['package'], journal=journal)
            for row in self.installed() if row['digest'] in active)

    def catalog(self, package, *, journal, timeout=10):
        path = Path(package).resolve()
        if not path.is_relative_to(self.root) or path.parent != self.root:
            raise PluginError('PROCESS_PACKAGE_NOT_INSTALLED')
        record = next((row for row in self.installed() if row['package'] == str(path)), None)
        if record is None or any(not (path / name).resolve().is_relative_to(path)
                or not (path / name).is_file()
                or sha256((path / name).read_bytes()).hexdigest() != expected
                for name, expected in record['contents']):
            raise PluginError('PROCESS_PACKAGE_CONTENT_CHANGED')
        manifest = _document(path / 'plugin.json')
        def mount(ctx, config):
            transport = ProcessTransport(path, journal=journal, timeout=config.get('timeout', timeout),
                identity=dict(ctx.identity))
            transport.on_exit = lambda: native_call('runtime_update', host=ctx.host.id,
                plugin=ctx.plugin_id, enabled=False)
            ctx.defer(transport.close)
            transport.start()
            for service, methods in manifest['services'].items():
                ctx.provide(service, ProcessService(transport, service, tuple(methods), ctx))
        return PluginSpec(f'process.{manifest["name"]}', tuple(manifest['services']), (), mount,
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


class ProcessTransport:
    def __init__(self, package, *, journal, timeout, identity):
        if type(timeout) not in {int, float} or not 0 < timeout <= 300:
            raise PluginError('INVALID_PROCESS_TIMEOUT')
        self.package, self.timeout = Path(package).resolve(), timeout
        self.manifest = _document(self.package / 'plugin.json')
        self.identity, self.session = identity, uuid4().hex
        self.journal = Path(journal).resolve()
        self.journal.mkdir(parents=True, exist_ok=True)
        self._lock, self._write_lock = RLock(), RLock()
        self._pending, self._process, self._reader = {}, None, None
        self._received = {}
        self._closed, self._draining, self._failure = False, False, None
        self.on_exit = None

    def _record(self, request, status, **fields):
        with (self.journal / f'{request["id"]}-{status}.json').open('x', encoding='utf-8') as stream:
            json.dump({'protocol': PROTOCOL, 'identity': self.identity, 'session': self.session,
                'request': request, 'status': status, **fields}, stream, ensure_ascii=False, allow_nan=False)

    def start(self):
        with self._lock:
            if self._process is not None:
                raise PluginError('PROCESS_ALREADY_STARTED')
            self._process = subprocess.Popen([self.manifest.get('python', sys.executable), '-I', '-u',
                '-c', PROCESS_LAUNCHER, str(self.package), str(self.package / self.manifest['entry'])], cwd=self.package,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._reader = Thread(target=self._read, daemon=True)
            self._reader.start()
        response = self.request('handshake', capabilities=self.manifest['services'])
        if (not isinstance(response, dict) or response.get('protocol') != PROTOCOL
                or response.get('services') != self.manifest['services']
                or not isinstance(response.get('runtime'), dict)
                or not isinstance(response['runtime'].get('python_version'), str)
                or not isinstance(response['runtime'].get('executable'), str)):
            raise PluginError('PROCESS_HANDSHAKE_MISMATCH')
        self.runtime_info = response['runtime']

    def _read(self):
        try:
            while True:
                line = self._process.stdout.readline(MAX_MESSAGE + 1)
                if not line:
                    raise PluginError('PROCESS_EXITED_UNKNOWN')
                if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
                    raise PluginError('PROCESS_MESSAGE_LIMIT')
                value = json.loads(line)
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
                try:
                    self.on_exit()
                except BaseException as failure:
                    exc.add_note(f'Runtime exit notification failed: {failure}')

    def request(self, kind, *, _cleanup=False, execution=None, cancel_signal=None, **fields):
        remaining = self.timeout
        if execution is not None and execution.get('deadline') is not None:
            remaining = min(remaining, execution['deadline'] / 1000 - monotonic())
            if remaining <= 0:
                raise PluginError('PROCESS_DEADLINE_BEFORE_SEND')
        request = {'protocol': PROTOCOL, 'id': uuid4().hex, 'kind': kind,
            'identity': execution if execution is not None else self.identity,
            'deadline_seconds': remaining, 'cancelled': False, **fields}
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(payload) > MAX_MESSAGE:
            raise PluginError('PROCESS_MESSAGE_LIMIT')
        future = Future()
        with self._lock:
            if (self._closed or self._draining and not _cleanup or self._failure
                    or self._process is None or self._process.poll() is not None):
                raise PluginError('PROCESS_UNAVAILABLE')
            self._pending[request['id']] = future
        sent = False
        try:
            self._record(request, 'PREPARED')
            with self._write_lock:
                self._record(request, 'SENT')
                sent = True
                self._process.stdin.write(payload)
                self._process.stdin.flush()
            expires, cancelled = monotonic() + remaining, False
            while True:
                if not cancelled and ((cancel_signal is not None and cancel_signal.is_set())
                        or monotonic() >= expires):
                    notification = {**request, 'kind': 'cancel', 'target': request['id'], 'cancelled': True}
                    with self._write_lock:
                        self._record(notification, 'CANCEL_REQUESTED')
                        self._process.stdin.write(json.dumps(notification).encode() + b'\n')
                        self._process.stdin.flush()
                    cancelled, expires = True, monotonic() + self.timeout
                try:
                    response = future.result(min(0.05, max(0.001, expires - monotonic())))
                    break
                except TimeoutError:
                    if cancelled and monotonic() >= expires:
                        raise
        except BaseException as exc:
            if not sent:
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
        if not response['ok']:
            self._record(request, 'ERROR', error=response.get('error'))
            raise PluginError('PROCESS_CALL_ERROR', str(response.get('error')))
        self._record(request, 'COMPLETE')
        return response.get('result')

    def _terminate(self):
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.timeout)

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
        if self._reader is not None:
            self._reader.join(timeout=self.timeout)
            if self._reader.is_alive():
                raise PluginError('PROCESS_READER_BUSY')
        if self._process is not None:
            self._process.stdin.close()
            self._process.stdout.close()
        with self._lock:
            self._closed = True
