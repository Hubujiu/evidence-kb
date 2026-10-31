import argparse
from hashlib import sha256
import io
import http.client
import json
import os
from pathlib import Path
import subprocess
import socket
import signal
import sys
from threading import Event
from time import monotonic
import unittest


ROOT = Path(__file__).resolve().parents[2]


def process_running(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform != 'linux':
        return True
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


class HostIntegration(unittest.TestCase):
    output = None
    captures = []

    def setUp(self):
        self.directory = self.output / sha256(self.id().encode('utf-8')).hexdigest()[:12]
        self.directory.mkdir()
        self.events = self.directory / 'events.jsonl'

    def profile(self, factory, **config):
        path = self.directory / 'profile.json'
        path.write_text(json.dumps({'version': 1,
            'worker': [sys.executable, '-B', '-m', 'kb_platform.host_worker'],
            'cwd': os.path.relpath(ROOT, self.directory),
            'application': {'factory': 'kb_platform.rag_testing.host_fixtures:' + factory,
                            'config': {'events': str(self.events), **config}},
            'startup_timeout_ms': 15000, 'shutdown_timeout_ms': 5000}), 'utf-8')
        return path

    def launch(self, factory, *, no_library=False, **config):
        environment = dict(os.environ)
        if no_library:
            environment['KB_AGENT_KERNEL_LIBRARY'] = str(self.directory / 'does-not-exist.dll')
        command = [os.environ['KB_PLUGIN_HOST_BINARY'], '--profile', str(self.profile(factory, **config))]
        completed = subprocess.run(command, cwd=ROOT, env=environment, text=True, encoding='utf-8',
                                   capture_output=True, timeout=35, check=False)
        self.capture(completed.returncode, completed.stdout, completed.stderr)
        return completed

    def capture(self, code, stdout, stderr):
        (self.directory / 'stdout.log').write_text(stdout, 'utf-8')
        (self.directory / 'stderr.log').write_text(stderr, 'utf-8')
        self.captures.append({'test': self.id(), 'exit_code': code,
                              'directory': self.directory.name})

    def records(self):
        return [json.loads(line) for line in self.events.read_text('utf-8').splitlines()] if self.events.exists() else []

    def successful(self, completed):
        self.assertEqual(0, completed.returncode, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertEqual('completed', document['status'])
        self.assertEqual(1, sum(row['event'] == 'closed' for row in self.records()))
        return document

    def test_generic_concurrent_nested_calls_and_scopes_need_no_library(self):
        document = self.successful(self.launch('generic', no_library=True))
        result = document['result']
        self.assertEqual(list(range(1, 32, 2)), result['values'])
        self.assertEqual(16, result['scoped'])
        self.assertEqual(11, result['parent'])
        self.assertFalse(result['leases'])
        self.assertNotEqual(document['runtime_owner_pid'], result['worker_pid'])
        disposed = [row['plugin'] for row in self.records() if row['event'] == 'disposed']
        self.assertCountEqual(['base', 'dependent', 'override', 'child-dependent'], disposed)
        self.assertLess(disposed.index('dependent'), disposed.index('base'))
        self.assertLess(disposed.index('child-dependent'), disposed.index('override'))

    def test_non_python_worker_uses_same_parent_runtime_protocol(self):
        profile = self.profile('generic')
        document = json.loads(profile.read_text('utf-8'))
        document['worker'] = [os.environ['KB_PLUGIN_HOST_NATIVE_WORKER']]
        profile.write_text(json.dumps(document), 'utf-8')
        completed = subprocess.run([os.environ['KB_PLUGIN_HOST_BINARY'], '--profile', str(profile)],
            cwd=ROOT, text=True, encoding='utf-8', capture_output=True, timeout=10, check=False)
        self.capture(completed.returncode, completed.stdout, completed.stderr)
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual('rust', result['result']['worker'])
        self.assertEqual('ready', result['result']['state'])
        self.assertEqual(document['application'], result['result']['echo'])
        self.assertNotEqual(result['runtime_owner_pid'], result['result']['worker_pid'])

    def test_agent_tool_history_and_idempotent_session_replay(self):
        document = self.successful(self.launch('agent', store=str(self.directory / 'sessions')))
        result = document['result']
        self.assertEqual(result['first'], result['replay'])
        self.assertEqual('answer', result['first']['result'])
        self.assertEqual(2, result['second']['revision'])
        self.assertEqual(['one:1'], result['effects'])
        self.assertEqual(3, len(result['requests']))
        self.assertEqual(['user', 'assistant', 'tool', 'assistant', 'user'],
                          [row['role'] for row in result['requests'][-1]])
        self.assertNotEqual(document['runtime_owner_pid'], result['worker_pid'])

    def test_knowledge_component_capture_uses_unified_evidence_scorer(self):
        document = self.successful(self.launch('knowledge',
            suite=str(ROOT / 'config/rag-testing/suite-v1.json'),
            scoring_output=str(self.directory / 'evidence-scoring')))
        result = document['result']
        self.assertEqual('PASS', result['decision'])
        self.assertEqual(1, result['total'])
        self.assertEqual('pre_send', result['capture'])
        self.assertGreater(result['messages'], 0)
        self.assertEqual(0, result['new_provider_calls'])

    def test_auxiliary_plugins_share_llm_route_and_physical_request_ledger(self):
        document = self.successful(self.launch('shared_model', ledger=str(self.directory / 'budget.sqlite3')))
        result = document['result']
        self.assertEqual({'text': 'scripted'}, result['title'])
        self.assertEqual({'text': 'scripted'}, result['compact'])
        self.assertEqual(['shared', 'shared'], [request['model'] for request in result['requests']])
        self.assertEqual(2, result['ledger']['physical_requests'])
        self.assertEqual(0, result['ledger']['pending_or_unknown'])

    def test_process_plugin_dependency_callbacks_share_rust_parent_lifecycle(self):
        document = self.successful(self.launch('process_dependency', no_library=True,
            workspace=str(self.directory / 'process-package')))
        result = document['result']
        self.assertEqual('PASS', result['decision'])
        self.assertEqual({'answer': 'hello:continued', 'model': 'deterministic-fixture'},
                          result['result']['result'])
        self.assertEqual('UNDECLARED_SERVICE_DEPENDENCY', result['undeclared_dependency']['error']['code'])
        self.assertFalse(result['remaining_leases'])
        self.assertEqual(2, len(result['callbacks']))
        self.assertTrue(all(row['same_thread'] for row in result['callbacks']))
        self.assertTrue(all(row['same_thread'] for row in result['provider_calls']))
        self.assertTrue(all(row['error'] == 'PROCESS_RESULT_UNKNOWN' for row in result['malformed_callbacks']))
        self.assertNotEqual(document['runtime_owner_pid'], result['worker_pid'])

    def test_process_plugin_calls_real_shared_llm_json_completion(self):
        document = self.successful(self.launch('process_llm', no_library=True,
            workspace=str(self.directory / 'process-package'), ledger=str(self.directory / 'budget.sqlite3')))
        result = document['result']
        completion = result['completion']
        self.assertEqual({'text': 'scripted'}, completion['value'])
        self.assertEqual(8, completion['usage']['total_tokens'])
        self.assertEqual(1, completion['attempts'])
        self.assertEqual('stop', completion['finish_reason'])
        self.assertEqual('none', completion['cache_kind'])
        self.assertTrue(completion['request_id'])
        self.assertTrue(completion['request_hash'])
        self.assertEqual(['shared'], [request['model'] for request in result['requests']])
        self.assertEqual(1, result['ledger']['physical_requests'])
        self.assertEqual(0, result['ledger']['pending_or_unknown'])
        self.assertFalse(result['remaining_leases'])
        self.assertEqual(0, result['model_routes']['generation']['active_calls'])

    def test_startup_failure_is_not_reported_as_completion(self):
        completed = self.launch('failure', phase='startup')
        self.assertNotEqual(0, completed.returncode)
        self.assertIn('HOST_FIXTURE_STARTUP_FAILURE', completed.stderr)
        self.assertEqual('', completed.stdout.strip())

    def check_process_tree(self, phase):
        receipt = self.directory / 'processes.json'
        profile = self.profile('generic')
        document = json.loads(profile.read_text('utf-8'))
        document['worker'] = [sys.executable, '-m', 'kb_platform.rag_testing.host_fixtures',
                              str(receipt), phase]
        document['startup_timeout_ms'] = 2000
        profile.write_text(json.dumps(document), 'utf-8')
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        owned = {}
        try:
            completed = subprocess.run([os.environ['KB_PLUGIN_HOST_BINARY'], '--profile', str(profile)],
                cwd=ROOT, text=True, encoding='utf-8', capture_output=True, timeout=10, check=False)
            self.capture(completed.returncode, completed.stdout, completed.stderr)
            owned = json.loads(receipt.read_text('utf-8'))
            expires = monotonic() + 2
            while monotonic() < expires and any(process_running(pid) for pid in owned.values()):
                Event().wait(0.02)
            observed = {name: process_running(pid) for name, pid in owned.items()}
            (self.directory / 'liveness.json').write_text(json.dumps(observed), 'utf-8')
            self.assertIsNone(unrelated.poll())
            self.assertEqual({'worker': False, 'descendant': False}, observed)
            if phase == 'completed':
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual('completed', json.loads(completed.stdout)['status'])
            else:
                self.assertNotEqual(0, completed.returncode)
                expected = 'transport' if phase == 'malformed' else 'timeout'
                self.assertEqual(expected, json.loads(completed.stderr.splitlines()[-1])['code'])
        finally:
            for pid in owned.values():
                if process_running(pid):
                    os.kill(pid, signal.SIGTERM)
            unrelated.kill()
            unrelated.wait(timeout=5)

    def test_malformed_worker_cannot_leave_descendants(self):
        self.check_process_tree('malformed')

    def test_timed_out_worker_cannot_leave_descendants(self):
        self.check_process_tree('timeout')

    def test_completed_worker_cannot_leave_descendants(self):
        self.check_process_tree('completed')

    def test_failed_mount_rolls_back_parent_owned_plugin_resources(self):
        completed = self.launch('failure', phase='mount', no_library=True)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn('HOST_FIXTURE_MOUNT_FAILURE', completed.stderr)
        self.assertEqual(['created', 'mounting', 'rolled_back'], [row['event'] for row in self.records()])
        error = json.loads(completed.stderr.splitlines()[-1])
        self.assertEqual([], error['roots'])

    def test_run_failure_still_closes_application(self):
        completed = self.launch('failure', phase='run')
        self.assertNotEqual(0, completed.returncode)
        self.assertIn('HOST_FIXTURE_RUN_FAILURE', completed.stderr)
        self.assertEqual(['created', 'run', 'closed'], [row['event'] for row in self.records()])

    def test_cleanup_failure_cannot_publish_success(self):
        completed = self.launch('failure', phase='close')
        self.assertNotEqual(0, completed.returncode)
        self.assertIn('HOST_FIXTURE_CLOSE_FAILURE', completed.stderr)
        self.assertEqual('', completed.stdout.strip())
        self.assertEqual(['created', 'run', 'closed'], [row['event'] for row in self.records()])

    def test_stop_file_cancels_active_plugin_before_cleanup(self):
        stop = self.directory / 'stop'
        command = [os.environ['KB_PLUGIN_HOST_BINARY'], '--profile', str(self.profile('cancellation')),
                   '--stop-file', str(stop)]
        process = subprocess.Popen(command, cwd=ROOT, text=True, encoding='utf-8',
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            expires = monotonic() + 15
            while monotonic() < expires and process.poll() is None:
                if any(row['event'] == 'running' for row in self.records()):
                    break
                Event().wait(0.02)
            self.assertTrue(any(row['event'] == 'running' for row in self.records()))
            stop.touch()
            stdout, stderr = process.communicate(timeout=10)
            self.capture(process.returncode, stdout, stderr)
            self.assertEqual(0, process.returncode, stderr)
            events = [row['event'] for row in self.records()]
            self.assertEqual(['created', 'running', 'stop', 'cancelled', 'disposed', 'closed'], events)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_production_http_application_serves_health_and_stops_cleanly(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        profile = self.profile('generic')
        document = json.loads(profile.read_text('utf-8'))
        document['application'] = {'factory': 'kb_platform.host_applications:api', 'config': {
            'profile': str(ROOT / 'config/application-profiles/native.json'),
            'host': '127.0.0.1', 'port': port, 'log_level': 'error'}}
        profile.write_text(json.dumps(document), 'utf-8')
        identities = self.directory / 'identities.json'
        identities.write_text(json.dumps({'synthetic-host-integration-credential': {
            'tenant_id': 'fixture', 'principal_id': 'owner'}}), 'utf-8')
        stop = self.directory / 'stop'
        environment = {**os.environ, 'KB_IDENTITIES_FILE': str(identities),
                       'KB_STORAGE_ROOT': str(self.directory / 'storage'), 'KB_REMOTE_MODELS': '0'}
        process = subprocess.Popen([os.environ['KB_PLUGIN_HOST_BINARY'], '--profile', str(profile),
            '--stop-file', str(stop)], cwd=ROOT, env=environment, text=True, encoding='utf-8',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        health = None
        try:
            expires = monotonic() + 15
            while monotonic() < expires and process.poll() is None:
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
                try:
                    connection.request('GET', '/health')
                    response = connection.getresponse()
                    health = {'status_code': response.status, 'body': json.loads(response.read())}
                    break
                except (OSError, http.client.HTTPException):
                    Event().wait(0.02)
                finally:
                    connection.close()
            stop.touch()
            stdout, stderr = process.communicate(timeout=10)
            self.capture(process.returncode, stdout, stderr)
            (self.directory / 'health.json').write_text(json.dumps(health), 'utf-8')
            self.assertEqual(0, process.returncode, stderr)
            self.assertEqual({'status_code': 200, 'body': {
                'status': 'running', 'profile': 'native', 'capabilities': []}}, health)
            outcome = json.loads(stdout)
            self.assertEqual('completed', outcome['status'])
            self.assertEqual({'status': 'stopped', 'port': port}, outcome['result'])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    if os.name == 'posix':
        def check_shutdown_signal(self, signum, *, hang_close=False, stop_file=False):
            profile = self.profile('signal_application', hang_close=hang_close)
            document = json.loads(profile.read_text('utf-8'))
            document['shutdown_timeout_ms'] = 500 if hang_close else 5000
            if stop_file:
                document['stop_file'] = str(self.directory / 'unused-stop')
            profile.write_text(json.dumps(document), 'utf-8')
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            process = subprocess.Popen([os.environ['KB_PLUGIN_HOST_BINARY'], '--profile', str(profile)],
                cwd=ROOT, text=True, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
            owned = {}
            try:
                expires = monotonic() + 15
                while monotonic() < expires and process.poll() is None:
                    running = [row for row in self.records() if row['event'] == 'running']
                    if running:
                        owned = {name: running[-1][name] for name in ('worker', 'descendant')}
                        break
                    Event().wait(0.02)
                self.assertTrue(owned, 'Application did not start')
                self.assertEqual(owned['worker'], os.getpgid(owned['descendant']))
                self.assertNotEqual(process.pid, os.getpgid(owned['worker']))
                self.assertTrue(all(process_running(pid) for pid in owned.values()))
                if signum == signal.SIGINT:
                    os.killpg(process.pid, signum)
                else:
                    process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=10)
                self.capture(process.returncode, stdout, stderr)
                expires = monotonic() + 2
                while monotonic() < expires and any(process_running(pid) for pid in owned.values()):
                    Event().wait(0.02)
                observed = {name: process_running(pid) for name, pid in owned.items()}
                (self.directory / 'liveness.json').write_text(json.dumps(observed), 'utf-8')
                self.assertEqual({'worker': False, 'descendant': False}, observed)
                self.assertIsNone(unrelated.poll())
                events = [row['event'] for row in self.records()]
                if hang_close:
                    self.assertNotEqual(0, process.returncode)
                    self.assertEqual('timeout', json.loads(stderr.splitlines()[-1])['code'])
                    self.assertEqual(['created', 'running', 'stop', 'closing'], events)
                else:
                    self.successful(subprocess.CompletedProcess([], process.returncode, stdout, stderr))
                    self.assertEqual(['created', 'running', 'stop', 'closing', 'closed'], events)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                if not owned:
                    running = [row for row in self.records() if row['event'] == 'running']
                    if running:
                        owned = {name: running[-1][name] for name in ('worker', 'descendant')}
                for pid in owned.values():
                    if process_running(pid):
                        os.kill(pid, signal.SIGKILL)
                unrelated.kill()
                unrelated.wait(timeout=5)

        def test_sigint_closes_application_and_reaps_descendants(self):
            self.check_shutdown_signal(signal.SIGINT)

        def test_sigterm_closes_application_and_reaps_descendants(self):
            self.check_shutdown_signal(signal.SIGTERM, stop_file=True)

        def test_sigint_reaps_descendants_when_close_times_out(self):
            self.check_shutdown_signal(signal.SIGINT, hang_close=True, stop_file=True)

        def test_sigterm_reaps_descendants_when_close_times_out(self):
            self.check_shutdown_signal(signal.SIGTERM, hang_close=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    binary = Path(os.environ['KB_PLUGIN_HOST_BINARY'])
    if not binary.is_file():
        raise RuntimeError('CURRENT_HOST_BINARY_REQUIRED')
    HostIntegration.output = args.output.resolve()
    HostIntegration.captures = []
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HostIntegration)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print(stream.getvalue())
    report = {'decision': 'PASS' if result.wasSuccessful() and not result.skipped else 'FAIL',
        'total': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'captures': HostIntegration.captures,
        'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0,
        'scope': 'Rust main host with trusted Python worker and controlled localhost HTTP smoke; '
                 'synthetic component fixtures and unified evidence scoring; '
                 'no live provider or production RAG quality claim; per-run Agent value kernel uses current DLL'}
    (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return 0 if report['decision'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
