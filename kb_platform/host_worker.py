import importlib
import os
import platform
import sys
from threading import Event, Lock, Thread

from .host_protocol import HostConnection, HostProtocolError, PipeReader, read_frame
from .plugin_native import install_transport


class ApplicationWorker:
    def __init__(self, connection):
        self.connection = connection
        self._lock = Lock()
        self._phase = 'new'
        self._last_request = 0
        self._application = None
        self._finishing = False
        self._stop_done = Event()
        self._stop_done.set()
        self._settled = Event()
        self._shutdown_responding = Event()
        self.stopped = Event()

    def _claim(self, method):
        transitions = {'handshake': ('new', 'handshaken'), 'boot': ('handshaken', 'booting'),
                       'run': ('ready', 'running')}
        with self._lock:
            if method == 'shutdown':
                if self._phase not in {'ready', 'running', 'settled', 'failed', 'handshaken'}:
                    raise HostProtocolError('HOST_INVALID_WORKER_STATE', self._phase)
                previous, self._phase = self._phase, 'stopping'
                return previous
            if method not in transitions or self._phase != transitions[method][0]:
                raise HostProtocolError('HOST_INVALID_WORKER_STATE', f'{self._phase}/{method}')
            self._phase = transitions[method][1]
            return None

    def accept(self, frame):
        if frame['id'] <= self._last_request:
            raise HostProtocolError('HOST_REPEATED_REQUEST')
        self._last_request = frame['id']
        try:
            previous = self._claim(frame['method'])
        except HostProtocolError as exc:
            self.connection.reply(frame['id'], error=exc)
            return
        Thread(target=self._execute, args=(frame, previous), daemon=True).start()

    def _boot(self, params):
        if (not isinstance(params, dict) or set(params) != {'factory', 'config'}
                or not isinstance(params['factory'], str) or not isinstance(params['config'], dict)):
            raise HostProtocolError('HOST_INVALID_APPLICATION')
        module, separator, name = params['factory'].partition(':')
        if not separator or not module or not name or ':' in name:
            raise HostProtocolError('HOST_INVALID_FACTORY')
        factory = getattr(importlib.import_module(module), name)
        application = factory(params['config'])
        try:
            if not all(callable(getattr(application, method, None)) for method in ('run', 'close')):
                raise HostProtocolError('HOST_INVALID_APPLICATION_INTERFACE')
        except BaseException:
            close = getattr(application, 'close', None)
            if callable(close):
                close()
            raise
        self._application = application
        with self._lock:
            self._phase = 'ready'
        return {'ready': True}

    def _run(self):
        try:
            return self._application.run()
        finally:
            with self._lock:
                self._finishing = True
            self._stop_done.wait()
            self._application.close()

    def _shutdown(self, previous):
        if previous == 'running':
            with self._lock:
                signal_stop = not self._finishing
                if signal_stop:
                    self._stop_done.clear()
            if signal_stop:
                try:
                    stop = getattr(self._application, 'stop', None)
                    if callable(stop):
                        stop()
                finally:
                    self._stop_done.set()
            self._settled.wait()
        elif previous == 'ready':
            self._application.close()
        return {'stopped': True}

    def _execute(self, frame, previous):
        try:
            method, params = frame['method'], frame['params']
            if method == 'handshake':
                if params != {'runtime': 'plugin-runtime/1'}:
                    raise HostProtocolError('HOST_HANDSHAKE_MISMATCH')
                result = {'runtime': 'plugin-runtime/1', 'language': 'python',
                          'version': platform.python_version(), 'executable': sys.executable}
            elif method == 'boot':
                result = self._boot(params)
            elif method == 'run':
                result = self._run()
            else:
                result = self._shutdown(previous)
            if method == 'shutdown':
                self._shutdown_responding.set()
            self.connection.reply(frame['id'], result=result)
        except BaseException as exc:
            with self._lock:
                if self._phase != 'stopping':
                    self._phase = 'failed'
            try:
                if frame['method'] == 'shutdown':
                    self._shutdown_responding.set()
                self.connection.reply(frame['id'], error=exc)
            except BaseException as transport_error:
                self.connection.fail(transport_error)
        finally:
            if frame['method'] == 'run':
                with self._lock:
                    if self._phase != 'stopping':
                        self._phase = 'settled'
                self._settled.set()
            if frame['method'] == 'shutdown':
                self.stopped.set()


def receive_messages(reader, connection, worker):
    try:
        while not worker.stopped.is_set():
            frame = read_frame(reader)
            if frame['kind'] == 'response':
                connection.receive(frame)
            else:
                worker.accept(frame)
    except (OSError, EOFError) as exc:
        if isinstance(exc, EOFError) and worker._shutdown_responding.is_set():
            worker.stopped.wait()
            return
        connection.fail(exc)
        worker.stopped.set()


def main():
    reader, writer = PipeReader(sys.stdin.fileno()), sys.stdout.buffer
    sys.stdout = sys.stderr
    os.environ['KB_DEFER_DEFAULT_APP'] = '1'
    connection = HostConnection(writer)
    install_transport(lambda request: connection.request('native', request))
    worker = ApplicationWorker(connection)
    thread = Thread(target=receive_messages, args=(reader, connection, worker), daemon=True)
    thread.start()
    worker.stopped.wait()
    if connection._failure is not None:
        print(f'Python worker connection failed: {connection._failure}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
