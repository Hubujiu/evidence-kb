import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from kb_platform.database_contracts import DatabaseError
from kb_platform.database_providers.sqlite_guard import sqlite_guard
from kb_platform.provider_runtime import RequestReceipts, RuntimePolicyError


ROOT = Path(__file__).resolve().parents[2]
CRASH = """import os
from pathlib import Path
import sys
from kb_platform.provider_runtime import RequestReceipts
root, physical, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
receipts = RequestReceipts(root)
original = receipts._write_path
def interrupt(target, row):
    if phase == 'before_physical' and target.suffix == '.json':
        os._exit(73)
    original(target, row)
    if phase in {'after_physical', 'after_unknown'} and target.suffix == '.json':
        os._exit(73)
    if phase == 'after_terminal' and target.suffix == '.terminal':
        os._exit(73)
receipts._write_path = interrupt
receipts.finish('logical', physical, 'UNKNOWN' if phase == 'after_unknown' else 'COMPLETE')
raise RuntimeError('CRASH_PHASE_NOT_REACHED')
"""
HELD = """from pathlib import Path
import sys
from kb_platform.provider_runtime import RequestReceipts
root, physical = Path(sys.argv[1]), sys.argv[2]
receipts = RequestReceipts(root)
original = receipts._write
def pause(identity, row):
    (root / 'ready').write_text('ready', 'utf-8')
    if sys.stdin.readline().strip() != 'release':
        raise RuntimeError('RECEIPT_RELEASE_REQUIRED')
    original(identity, row)
receipts._write = pause
receipts.finish('logical', physical, 'COMPLETE')
"""


class ReceiptProcessIntegration(unittest.TestCase):
    def test_child_process_guard_excludes_parent_and_releases_after_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            receipts = RequestReceipts(root)
            physical = receipts.begin('logical', 'payload', {})
            child = subprocess.Popen([sys.executable, '-c', HELD, directory, physical], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while not (root / 'ready').exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / 'ready').exists())
                guard = receipts._pending('logical').with_suffix('.guard.sqlite3')
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_GUARD_BUSY'):
                    with sqlite_guard(guard):
                        self.fail('parent acquired child process guard')
                independent = receipts.begin('independent', 'payload', {})
                receipts.finish('independent', independent, 'ERROR')
                output, error = child.communicate('release\n', timeout=10)
                self.assertEqual(child.returncode, 0, output + error)
                with sqlite_guard(guard):
                    self.assertFalse(receipts._pending('logical').exists())
                with self.assertRaisesRegex(RuntimePolicyError, 'INVALID_PHYSICAL_REQUEST_TRANSITION'):
                    receipts.finish('logical', physical, 'UNKNOWN')
                row = json.loads((root / (physical + '.json')).read_text('utf-8'))
                self.assertEqual(row['status'], 'COMPLETE')
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_process_death_releases_guard_without_releasing_unresolved_receipt(self):
        for phase in ('before_physical', 'after_physical', 'after_terminal', 'after_unknown'):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory)
                receipts = RequestReceipts(root)
                physical = receipts.begin('logical', 'payload', {})
                result = subprocess.run([sys.executable, '-c', CRASH, directory, physical, phase], cwd=ROOT,
                    text=True, capture_output=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
                reopened = RequestReceipts(root)
                pending = reopened._pending('logical')
                with sqlite_guard(pending.with_suffix('.guard.sqlite3')):
                    self.assertEqual(json.loads(pending.read_text('utf-8'))['physical_request_id'], physical)
                with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
                    reopened.begin('logical', 'payload', {'retry_of': physical, 'retry_reason': 'explicit'})
                row = json.loads((root / (physical + '.json')).read_text('utf-8'))
                expected = 'STARTED' if phase == 'before_physical' else 'UNKNOWN' if phase == 'after_unknown' else 'COMPLETE'
                self.assertEqual(row['status'], expected)
                self.assertTrue(pending.exists())
                self.assertTrue(pending.with_suffix('.guard.sqlite3').exists())
