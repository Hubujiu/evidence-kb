import argparse
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from scripts import delivery_control as control


HOST_CODE = '''
import os, subprocess, sys, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
server = None
if os.environ.get("FIXTURE_SERVE") == "1":
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("X-Delivery-Profile", "fixture")
            self.send_header("X-Delivery-Instance", os.environ["KB_DELIVERY_INSTANCE"])
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", int(os.environ["FIXTURE_PORT"])), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(os.environ["FIXTURE_WORKER"]).write_text(str(child.pid))
try:
    while not Path(sys.argv[1]).exists():
        time.sleep(.01)
finally:
    if server:
        server.shutdown()
        server.server_close()
    child.terminate()
    child.wait(timeout=3)
'''

SERVER_CODE = '''
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("X-Delivery-Profile", "fixture")
        self.end_headers()
    def log_message(self, *args):
        pass
server = HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
Path(sys.argv[2]).touch()
server.serve_forever()
'''


def wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f'fixture did not create {path.name}')
        time.sleep(.01)


def alive(fd):
    poll = select.poll()
    poll.register(fd, select.POLLIN)
    return not poll.poll(0)


def stop_pidfd(fd):
    if alive(fd):
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
    poll = select.poll()
    poll.register(fd, select.POLLIN)
    if not poll.poll(3000):
        raise AssertionError('owned fixture process did not exit')
    os.close(fd)


class Fixture:
    def __init__(self, root):
        self.root = root
        self.worker = root / 'worker.pid'
        self.spawned = []
        self.worker_fd = None
        self.server = None
        self.real_popen = subprocess.Popen
        self.real_pidfd = os.pidfd_open
        self.real_write = os.write
        self.real_sleep = time.sleep
        (root / '.venv/bin').mkdir(parents=True)
        (root / '.venv/bin/python').symlink_to(Path(sys.executable).resolve())
        for name in ('config.json', 'providers.json', 'semantic.json', 'bin/libagent_kernel.so'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"id":"fixture"}' if name == 'config.json' else '', encoding='utf-8')
        self.host = Path(sys.executable).resolve()
        self.real_required_file = control.required_file
        self.args = argparse.Namespace(config='config.json', providers='providers.json',
            semantic_runtime='semantic.json', run_root='runs', budget_batch='', check_only=False,
            host_binary=str(self.host), start_dependencies=False)
        self.sentinel = self.real_popen([sys.executable, '-c', 'import time;time.sleep(60)'])

    def capture_worker(self):
        if self.worker_fd is None:
            wait_file(self.worker)
            self.worker_fd = self.real_pidfd(int(self.worker.read_text()))
        return self.worker_fd

    def launch(self, *args, **kwargs):
        command = list(args[0])
        original = 'os.execv(sys.argv[2],sys.argv[2:])'
        if original not in command[5]:
            raise AssertionError('launch no longer uses the expected exec barrier')
        replacement = 'os.execv(sys.argv[2],[sys.argv[2],"-c",' + repr(HOST_CODE) + ',sys.argv[-1]])'
        command[5] = command[5].replace(original, replacement)
        env = dict(kwargs['env'])
        env['FIXTURE_WORKER'] = str(self.worker)
        env['FIXTURE_PORT'] = str(self.port)
        env['FIXTURE_SERVE'] = '1' if self.serve else '0'
        env['PYTHONHOME'] = sys.base_prefix
        lib = str(Path(sys.base_prefix) / 'lib')
        env['LD_LIBRARY_PATH'] = lib + os.pathsep + env.get('LD_LIBRARY_PATH', '')
        kwargs['env'] = env
        child = self.real_popen(command, **kwargs)
        self.spawned.append(child)
        return child

    def start(self, *, pidfd_failure=False, identity_failure=False, receipt_failure=False,
              interrupt_after_release=False, wrong_server=False, serve=False):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.port, self.serve = port, serve
        def open_pidfd(pid):
            if pidfd_failure and pid != os.getpid():
                raise OSError('injected pidfd failure')
            return self.real_pidfd(pid)
        def write_fd(fd, data):
            result = self.real_write(fd, data)
            if interrupt_after_release and data == b'1':
                self.capture_worker()
                raise KeyboardInterrupt('injected after release')
            return result
        def launch(*args, **kwargs):
            if wrong_server:
                ready = self.root / 'server-ready'
                self.server = self.real_popen([sys.executable, '-c', SERVER_CODE, str(port), str(ready)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wait_file(ready)
            return self.launch(*args, **kwargs)
        def required(value):
            if str(value).endswith('/libagent_kernel.so'):
                return self.root / 'bin/libagent_kernel.so'
            return self.real_required_file(value)
        with patch.object(control, 'ROOT', self.root), patch.object(control, 'PORT', port), \
                patch.object(control, 'required_file', side_effect=required), \
                patch.object(control.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch.object(control.subprocess, 'Popen', side_effect=launch), \
                patch.object(control.os, 'pidfd_open', side_effect=open_pidfd), \
                patch.object(control.os, 'write', side_effect=write_fd):
            with patch.object(control, 'process_start', side_effect=OSError('injected identity failure')) if identity_failure else _null():
                with patch.object(control, 'write_receipt', side_effect=OSError('injected receipt failure')) if receipt_failure else _null():
                    if wrong_server:
                        with patch.object(control.time, 'sleep', return_value=None):
                            return control.start(self.args)
                    return control.start(self.args)

    def receipt(self):
        receipts = list((self.root / 'runs').glob('*/process.json'))
        if len(receipts) != 1:
            raise AssertionError(f'expected one receipt, found {len(receipts)}')
        return receipts[0], json.loads(receipts[0].read_text(encoding='utf-8'))

    def close(self):
        try:
            if self.worker_fd is None and self.worker.exists():
                self.worker_fd = self.real_pidfd(int(self.worker.read_text()))
            if self.worker_fd is not None:
                stop_pidfd(self.worker_fd)
            for child in self.spawned:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=3)
            if self.server is not None:
                self.server.terminate()
                self.server.wait(timeout=3)
            if self.sentinel.poll() is not None:
                raise AssertionError('unrelated sentinel was affected')
        finally:
            if self.sentinel.poll() is None:
                self.sentinel.terminate()
            self.sentinel.wait(timeout=3)


class _null:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def startup_failures(root):
    for failure in ('pidfd_failure', 'identity_failure', 'receipt_failure'):
        directory = root / failure
        directory.mkdir()
        fixture = Fixture(directory)
        try:
            try:
                fixture.start(**{failure: True})
            except OSError:
                pass
            else:
                raise AssertionError(f'{failure} did not fail')
            if fixture.worker.exists() or any(child.poll() is None for child in fixture.spawned):
                raise AssertionError('pre-release failure executed host or retained wrapper')
        finally:
            fixture.close()


def post_release_interrupt(root):
    interrupted = root / 'interrupted'
    interrupted.mkdir()
    fixture = Fixture(interrupted)
    try:
        try:
            fixture.start(interrupt_after_release=True)
        except RuntimeError as exc:
            if 'inspect receipt' not in str(exc):
                raise
        else:
            raise AssertionError('interruption was lost')
        path, receipt = fixture.receipt()
        if receipt['status'] != 'STARTING' or fixture.spawned[0].poll() is not None:
            raise AssertionError('published host or receipt was lost')
        if not alive(fixture.capture_worker()):
            raise AssertionError('released worker was killed')
        control.stop(argparse.Namespace(receipt=str(path), timeout_seconds=3))
        if alive(fixture.worker_fd):
            raise AssertionError('worker survived graceful host stop')
    finally:
        fixture.close()
    normal = root / 'normal'
    normal.mkdir()
    fixture = Fixture(normal)
    try:
        fixture.start(serve=True)
        path, receipt = fixture.receipt()
        if receipt['status'] != 'RUNNING_LIVENESS_ONLY' or fixture.spawned[0].poll() is not None:
            raise AssertionError('matching instance did not start')
        fixture.capture_worker()
        control.stop(argparse.Namespace(receipt=str(path), timeout_seconds=3))
        if alive(fixture.worker_fd):
            raise AssertionError('worker survived normal host stop')
    finally:
        fixture.close()


def stop_marker(root):
    run = root / 'run'
    run.mkdir()
    outside = root / 'unrelated'
    outside.write_text('ORIGINAL', encoding='utf-8')
    os.link(outside, run / 'stop.request')
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    try:
        receipt = {'run': str(run), 'stop_file': str(run / 'stop.request'), 'pid': child.pid,
                   'process_start': control.process_start(child.pid),
                   'host_binary': str(Path(sys.executable).resolve())}
        path = run / 'process.json'
        path.write_text(json.dumps(receipt), encoding='utf-8')
        try:
            control.stop(argparse.Namespace(receipt=str(path), timeout_seconds=1))
        except ValueError as exc:
            if 'private regular file' not in str(exc):
                raise
        else:
            raise AssertionError('hard link accepted')
        if outside.read_text(encoding='utf-8') != 'ORIGINAL' or child.poll() is not None:
            raise AssertionError('unrelated content or child was affected')
    finally:
        child.terminate()
        child.wait(timeout=3)
    (run / 'stop.request').unlink()
    marker = run / 'stop.request'
    marker.write_text('existing marker', encoding='utf-8')
    release = root / 'release'
    child = subprocess.Popen([sys.executable, '-c',
        'import sys,time;from pathlib import Path;'
        'p=Path(sys.argv[1]);'
        'exec("while not p.exists():\\n time.sleep(.01)")', str(release)])
    try:
        receipt.update(pid=child.pid, process_start=control.process_start(child.pid))
        path.write_text(json.dumps(receipt), encoding='utf-8')
        actual_poll = select.poll
        class ReleasingPoll:
            def __init__(self):
                self.poller = actual_poll()
            def register(self, *args):
                self.poller.register(*args)
            def poll(self, timeout):
                if timeout:
                    release.touch()
                return self.poller.poll(timeout)
        with patch.object(control.select, 'poll', ReleasingPoll):
            control.stop(argparse.Namespace(receipt=str(path), timeout_seconds=3))
        child.wait(timeout=3)
        if marker.read_text(encoding='utf-8') != 'existing marker':
            raise AssertionError('existing stop marker was overwritten')
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=3)


def wrong_instance(root):
    fixture = Fixture(root)
    try:
        try:
            fixture.start(wrong_server=True)
        except ValueError as exc:
            if 'did not confirm readiness' not in str(exc):
                raise
        else:
            raise AssertionError('wrong instance was accepted')
        path, receipt = fixture.receipt()
        if receipt['status'] != 'STARTING' or fixture.spawned[0].poll() is not None:
            raise AssertionError('timed-out host has no recoverable receipt')
        fixture.capture_worker()
        control.stop(argparse.Namespace(receipt=str(path), timeout_seconds=3))
        if alive(fixture.worker_fd):
            raise AssertionError('worker survived graceful host stop')
    finally:
        fixture.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('case', choices=('startup_failures', 'post_release_interrupt', 'stop_marker', 'wrong_instance'))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='delivery-control-fixture-') as directory:
        globals()[args.case](Path(directory))
    print(f'{args.case}: PASS')


if __name__ == '__main__':
    main()
