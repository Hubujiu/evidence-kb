import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest

from scripts.check_process_owner import run_owned
from scripts.check_process_owner_witness import witness

ROOT = Path(__file__).resolve().parents[2]


class CheckProcessOwnerIntegration(unittest.TestCase):
    def check_tree(self, mode, *, nested=False):
        cancellation = Event()
        handles = []
        with tempfile.TemporaryDirectory(prefix='owned-gate-fixture-') as temporary:
            directory = Path(temporary)
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'])
            try:
                with (directory / 'owner.log').open('w') as stream, \
                        concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    command = [sys.executable, '-B', '-m',
                        'kb_platform.rag_testing.check_process_owner_fixture', str(directory),
                        'coordinator' if nested else 'child', 'exit' if mode == 'early_exit' else 'wait']
                    future = executor.submit(run_owned, command, cwd=ROOT, env=dict(os.environ),
                        stream=stream, timeout=3, cleanup_timeout=2, cancel_event=cancellation)
                    try:
                        deadline = time.monotonic() + 2
                        for role in (('coordinator', 'nested-owner', 'child', 'grandchild')
                                     if nested else ('child', 'grandchild')):
                            receipt = directory / f'{role}.json'
                            while not receipt.exists() and time.monotonic() < deadline and not future.done():
                                time.sleep(0.01)
                            self.assertTrue(receipt.exists(), 'Owned fixture did not reach process creation')
                            record = json.loads(receipt.read_text('utf-8'))
                            handle = witness(record['pid'], record['birth'])
                            handles.append(handle)
                            self.assertTrue(handle.running())
                        (directory / 'release').touch()
                        if mode == 'cancel':
                            cancellation.set()
                        result = future.result(timeout=8)
                        self.assertTrue(result.cleanup_complete, result)
                        self.assertTrue(all(not handle.running() for handle in handles))
                        self.assertIsNone(sentinel.poll())
                        self.assertEqual(mode == 'early_exit', result.passed)
                        self.assertEqual(mode == 'timeout', result.timed_out)
                        self.assertEqual(mode == 'cancel', result.cancelled)
                        self.assertLessEqual(result.cleanup_seconds, 2.5)
                    finally:
                        cancellation.set()
                        for handle in handles:
                            handle.terminate()
                            self.assertTrue(handle.wait(time.monotonic() + 2))
                            handle.close()
            finally:
                sentinel.kill()
                sentinel.wait(timeout=3)

    def test_timeout_cleans_independent_session_descendants_not_sentinel(self):
        self.check_tree('timeout')

    def test_successful_child_exit_still_cleans_descendants(self):
        self.check_tree('early_exit')

    def test_cancellation_cleans_owned_tree_and_preserves_failure(self):
        self.check_tree('cancel')

    def test_nested_owner_cleanup_after_coordinator_exit(self):
        self.check_tree('early_exit', nested=True)

    def test_nested_owner_cleanup_after_cancellation(self):
        self.check_tree('cancel', nested=True)

    def test_pre_cancelled_payload_never_starts(self):
        cancellation = Event()
        cancellation.set()
        with tempfile.TemporaryDirectory(prefix='owned-gate-pre-cancel-') as directory:
            marker = Path(directory) / 'ran'
            with (Path(directory) / 'owner.log').open('w') as stream:
                result = run_owned([sys.executable, '-c',
                    'from pathlib import Path; Path(%r).touch()' % str(marker)],
                    cwd=ROOT, env=dict(os.environ), stream=stream, timeout=2,
                    cancel_event=cancellation)
            self.assertTrue(result.cancelled)
            self.assertFalse(result.passed)
            self.assertFalse(marker.exists())

    def test_large_environment_reaches_payload_without_control_variable_or_secret_logs(self):
        from scripts.check_process_owner import _CONFIG_ENV
        with tempfile.TemporaryDirectory(prefix='owned-gate-large-env-') as directory:
            directory = Path(directory)
            marker = directory / 'marker.json'
            env = dict(os.environ)
            env.update({f'OWNER_LARGE_{i}': 'sensitive-' + 'x' * 4096 for i in range(12)})
            code = ('import json,os,sys; from pathlib import Path; '
                    'Path(sys.argv[1]).write_text(json.dumps({'
                    '"preserved": all(os.environ.get("OWNER_LARGE_"+str(i)) == "sensitive-"+"x"*4096 for i in range(12)),'
                    '"control_absent": "EVIDENCE_KB_CHECK_OWNER_CONFIG" not in os.environ}))')
            with (directory / 'owner.log').open('w', encoding='utf-8') as stream:
                result = run_owned([sys.executable, '-c', code, str(marker)],
                    cwd=ROOT, env=env, stream=stream, timeout=10, cleanup_timeout=2)
            self.assertTrue(result.passed, result)
            self.assertEqual({'preserved': True, 'control_absent': True},
                             json.loads(marker.read_text('utf-8')))
            self.assertNotIn('sensitive-', (directory / 'owner.log').read_text('utf-8'))
            self.assertNotIn(_CONFIG_ENV, (directory / 'owner.log').read_text('utf-8'))

    def test_unresponsive_supervisor_cancellation_remains_owned(self):
        import textwrap
        with tempfile.TemporaryDirectory(prefix='owned-gate-stalled-supervisor-') as directory:
            directory = Path(directory)
            receipt = directory / 'sleeper.json'
            probe = textwrap.dedent("""
                import os, subprocess, sys, threading
                from pathlib import Path
                from scripts import check_process_owner as owner
                sleeper = ('import json,os,sys,time; '
                           'from scripts.check_process_owner_witness import own_birth; '
                           'from pathlib import Path; '
                           'Path(sys.argv[1]).write_text(json.dumps({"pid": os.getpid(), "birth": own_birth()})); '
                           'time.sleep(30)')
                real_popen = subprocess.Popen
                def stalled(*args, **kwargs):
                    return real_popen([sys.executable, '-B', '-c', sleeper, sys.argv[1]],
                        stdin=subprocess.PIPE, stdout=kwargs['stdout'], stderr=subprocess.STDOUT,
                        text=True, encoding='utf-8')
                owner.subprocess.Popen = stalled
                cancel = threading.Event()
                threading.Timer(1.5, cancel.set).start()
                env = dict(os.environ)
                env.update({f'OWNER_LARGE_{i}': 'x' * 4096 for i in range(12)})
                with open(os.devnull, 'w') as stream:
                    result = owner.run_owned(['ignored'], cwd=Path.cwd(), env=env, stream=stream,
                                             timeout=10, cleanup_timeout=0.1, cancel_event=cancel)
                print(result.error, result.cancelled, result.cleanup_complete, flush=True)
            """)
            handle = None
            with (directory / 'outer.log').open('w', encoding='utf-8') as stream, \
                    concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_owned, [sys.executable, '-B', '-c', probe, str(receipt)],
                    cwd=ROOT, env=dict(os.environ), stream=stream, timeout=12, cleanup_timeout=2)
                try:
                    deadline = time.monotonic() + 5
                    while not receipt.exists() and time.monotonic() < deadline and not future.done():
                        time.sleep(0.01)
                    self.assertTrue(receipt.exists(), 'sleeping supervisor did not start')
                    record = json.loads(receipt.read_text('utf-8'))
                    handle = witness(record['pid'], record['birth'])
                    result = future.result(timeout=15)
                    self.assertTrue(result.passed, result)
                    self.assertIn('OWNER_SUPERVISOR_WAIT_TIMEOUT True False',
                                  (directory / 'outer.log').read_text('utf-8'))
                    self.assertFalse(handle.running())
                finally:
                    if handle is not None:
                        handle.terminate()
                        self.assertTrue(handle.wait(time.monotonic() + 2))
                        handle.close()

    def test_confirmed_empty_tree_after_deadline_is_failure(self):
        from unittest.mock import patch
        from scripts.check_process_owner import backend, supervise

        class DelayedOwner(backend()):
            def cleanup(self, deadline):
                clean = super().cleanup(deadline)
                time.sleep(0.2)
                return clean
        config = dict(command=[sys.executable, '-c', 'pass'], cwd=str(ROOT),
                      env=dict(os.environ), timeout=3, cleanup_timeout=0.1)
        with patch('scripts.check_process_owner.backend', return_value=DelayedOwner):
            result = supervise(config, Event())
        self.assertTrue(result.cleanup_complete, result)
        self.assertEqual('CLEANUP_DEADLINE_EXCEEDED', result.error)
        self.assertFalse(result.passed)

    def test_spawn_failure_and_normal_exit_have_terminal_ownership(self):
        with tempfile.TemporaryDirectory(prefix='owned-gate-spawn-') as directory:
            with (Path(directory) / 'owner.log').open('w') as stream:
                missing = run_owned([str(Path(directory) / 'missing-executable')],
                    cwd=ROOT, env=dict(os.environ), stream=stream, timeout=2)
                self.assertFalse(missing.passed)
                self.assertIsNotNone(missing.error)
                normal = run_owned([sys.executable, '-c', 'pass'],
                    cwd=ROOT, env=dict(os.environ), stream=stream, timeout=2)
                self.assertTrue(normal.passed, normal)
                self.assertIsNotNone(normal.exit_observed_monotonic)
                self.assertIsNotNone(normal.cleanup_finished_monotonic)
                self.assertLessEqual(normal.exit_observed_monotonic, normal.cleanup_finished_monotonic)
