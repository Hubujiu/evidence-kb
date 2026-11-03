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
    def check_tree(self, mode):
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
                        'child', 'exit' if mode == 'early_exit' else 'wait']
                    future = executor.submit(run_owned, command, cwd=ROOT, env=dict(os.environ),
                        stream=stream, timeout=3, cleanup_timeout=2, cancel_event=cancellation)
                    try:
                        deadline = time.monotonic() + 2
                        for role in ('child', 'grandchild'):
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
