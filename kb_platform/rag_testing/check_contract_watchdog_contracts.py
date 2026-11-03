import tempfile
import time
import unittest
from scripts.check_contract_watchdog import SnapshotWatchdog, valid_capture


class SnapshotChecks(unittest.TestCase):
    def test_snapshot_contains_frames_but_never_local_values(self):
        secret = 'SENTINEL_SECRET_NOT_FOR_LOGS_9e32ea64'
        with tempfile.TemporaryFile() as stream:
            watchdog = SnapshotWatchdog(0.02, stream.fileno())
            time.sleep(0.1)
            status = watchdog.cancel()
            self.assertTrue(valid_capture(status, required=True), status)
            stream.seek(0)
            text = stream.read().decode('utf-8')
            self.assertIn('GIL_STACK_COMPLETE', text)
            self.assertIn('test_snapshot_contains_frames_but_never_local_values', text)
            self.assertNotIn(secret, text)

    def test_cancel_before_deadline_stops_without_later_output(self):
        with tempfile.TemporaryFile() as stream:
            watchdog = SnapshotWatchdog(0.2, stream.fileno())
            status = watchdog.cancel()
            self.assertTrue(valid_capture(status, required=False), status)
            self.assertFalse(status['triggered'])
            time.sleep(0.25)
            stream.seek(0)
            self.assertEqual(b'', stream.read())

    def test_write_failure_and_unfinished_thread_cannot_pass(self):
        watchdog = SnapshotWatchdog(0.02, -1)
        time.sleep(0.1)
        status = watchdog.cancel()
        self.assertEqual('OSError', status['error'])
        self.assertFalse(valid_capture(status, required=True))
        self.assertFalse(valid_capture({'thread_stopped': False, 'error': None, 'written': True,
                                        'due': True, 'triggered': True}, required=True))
        self.assertFalse(valid_capture({'thread_stopped': True, 'error': None, 'written': False,
                                        'due': True, 'triggered': False}, required=False))



class WorkerSnapshotFailureContracts(unittest.TestCase):
    def test_missing_due_snapshot_cannot_be_reported_as_worker_pass(self):
        from pathlib import Path
        from unittest.mock import patch
        from scripts import check_project

        case = unittest.FunctionTestCase(lambda: None)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(check_project.sys, 'addaudithook'), \
                patch.object(check_project, 'contract_modules', return_value=['fixture']), \
                patch.object(check_project.unittest.defaultTestLoader, 'loadTestsFromName',
                             return_value=unittest.TestSuite([case])), \
                patch('scripts.check_contract_watchdog.SnapshotWatchdog') as watchdog:
            watchdog.return_value.cancel.return_value = {
                'thread_stopped': True, 'error': None, 'written': False, 'due': True, 'triggered': False}
            with self.assertRaisesRegex(RuntimeError, 'CONTRACT_STACK_CAPTURE_UNCONFIRMED'):
                check_project.run_worker('contracts', Path(directory), 0)

    def test_snapshot_failure_preserves_an_existing_worker_exception(self):
        from pathlib import Path
        from unittest.mock import patch
        from scripts import check_project

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(check_project.sys, 'addaudithook'), \
                patch.object(check_project, 'contract_modules', side_effect=RuntimeError('original failure')), \
                patch('scripts.check_contract_watchdog.SnapshotWatchdog') as watchdog:
            watchdog.return_value.cancel.return_value = {
                'thread_stopped': True, 'error': 'OSError', 'written': False, 'due': True, 'triggered': True}
            with self.assertRaisesRegex(RuntimeError, 'original failure') as caught:
                check_project.run_worker('contracts', Path(directory), 0)
            self.assertTrue(any('CONTRACT_STACK_CAPTURE_UNCONFIRMED' in note for note in caught.exception.__notes__))

    def test_cleanup_exception_preserves_existing_worker_exception(self):
        from pathlib import Path
        from unittest.mock import patch
        from scripts import check_project

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(check_project.sys, 'addaudithook'), \
                patch.object(check_project, 'contract_modules', side_effect=RuntimeError('original failure')), \
                patch('scripts.check_contract_watchdog.SnapshotWatchdog') as watchdog:
            watchdog.return_value.cancel.side_effect = OSError('diagnostic failed')
            with self.assertRaisesRegex(RuntimeError, 'original failure') as caught:
                check_project.run_worker('contracts', Path(directory), 0)
            self.assertTrue(any('CONTRACT_STACK_CAPTURE_UNCONFIRMED' in note for note in caught.exception.__notes__))
