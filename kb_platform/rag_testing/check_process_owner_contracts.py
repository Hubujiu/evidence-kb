import unittest
from unittest.mock import patch

from scripts.check_process_owner import Outcome, run_owned, supervise
from scripts.check_process_owner_linux import Owner


class CheckProcessOwnerContracts(unittest.TestCase):
    def test_failure_facts_are_sticky_even_when_cleanup_succeeds(self):
        self.assertTrue(Outcome(0, False, False, True, 0, None).passed)
        for code, timeout, cancel, clean, error in ((2, False, False, True, None),
                (0, True, False, True, None), (0, False, True, True, None),
                (0, False, False, False, None), (0, False, False, True, 'failure')):
            self.assertFalse(Outcome(code, timeout, cancel, clean, 0, error).passed)

    def test_unsupported_platform_refuses_before_spawn(self):
        with patch('scripts.check_process_owner.sys.platform', 'darwin'), \
                patch('scripts.check_process_owner.subprocess.Popen') as spawn:
            result = run_owned([], cwd='.', env={}, stream=None, timeout=1)
        spawn.assert_not_called()
        self.assertEqual('UNSUPPORTED_PROCESS_CONTAINMENT', result.error)
        self.assertFalse(result.passed)

    def test_cleanup_failure_and_cancel_cannot_pass(self):
        from threading import Event
        events = []
        class FakeOwner:
            def spawn(self, *args):
                events.append('spawn')
            def poll(self):
                return 0
            def cleanup(self, deadline):
                events.append('cleanup')
                raise OSError('injected')
            def close(self):
                events.append('close')
        cancelled = Event()
        config = dict(command=[], cwd='.', env={}, timeout=1, cleanup_timeout=1)
        with patch('scripts.check_process_owner.backend', return_value=FakeOwner):
            result = supervise(config, cancelled)
            self.assertFalse(result.passed)
            self.assertFalse(result.cleanup_complete)
            self.assertEqual(['spawn', 'cleanup', 'close'], events)
            cancelled.set()
            result = supervise(config, cancelled)
            self.assertTrue(result.cancelled)
            self.assertFalse(result.passed)

    def test_linux_stale_generation_cannot_receive_owned_handle(self):
        owner = Owner.__new__(Owner)
        owner.handles = {123: ('old', 19)}
        with patch.object(owner, 'identity', return_value=(77, 'new')), \
                patch('scripts.check_process_owner_linux.os.getpid', return_value=77), \
                patch('scripts.check_process_owner_linux.os.pidfd_open', return_value=20, create=True), \
                patch('scripts.check_process_owner_linux.os.close') as close:
            with self.assertRaisesRegex(RuntimeError, 'CHILD_PID_REUSED'):
                owner.bind(123)
        close.assert_called_once_with(20)
        self.assertEqual({123: ('old', 19)}, owner.handles)

    def test_linux_empty_candidate_list_does_not_mean_no_children(self):
        owner = Owner.__new__(Owner)
        owner.failed = False
        class Children:
            def read_text(self):
                return ''
        owner.children = Children()
        with patch('scripts.check_process_owner_linux.os.waitpid', return_value=(0, 0)), \
                patch('scripts.check_process_owner_linux.time.monotonic', side_effect=[0, 2]), \
                patch('scripts.check_process_owner_linux.time.sleep'):
            self.assertFalse(owner.cleanup(1))
        with patch('scripts.check_process_owner_linux.os.waitpid', side_effect=ChildProcessError), \
                patch('scripts.check_process_owner_linux.time.monotonic', return_value=0):
            self.assertTrue(owner.cleanup(1))

    def test_linux_containment_capability_failure_precedes_payload(self):
        with patch('scripts.check_process_owner_linux.Path.read_text', side_effect=FileNotFoundError), \
                patch('scripts.check_process_owner_linux.subprocess.Popen') as spawn, \
                patch('scripts.check_process_owner_linux.os.pidfd_open', create=True), \
                patch('scripts.check_process_owner_linux.signal.pidfd_send_signal', create=True):
            with self.assertRaisesRegex(RuntimeError, 'OWNED_CHILD_DISCOVERY_UNAVAILABLE'):
                Owner()
        spawn.assert_not_called()

    def test_mandatory_lint_rejects_owner_bypass_and_weakened_evidence(self):
        from pathlib import Path
        from scripts.check_process_owner_lint import violations
        root = Path(__file__).resolve().parents[2]
        paths = ('scripts/check_project.py', 'scripts/check_process_owner.py',
                 'scripts/check_process_owner_linux.py', 'scripts/check_process_owner_windows.py',
                 'kb_platform/rag_testing/foundation_integration.py')
        sources = {path: (root / path).read_text('utf-8') for path in paths}
        self.assertEqual([], violations(root, sources))
        mutants = ((paths[1], 'and self.cleanup_complete', 'and True'),
            (paths[1], 'and not self.timed_out', 'and True'),
            (paths[1], 'and not self.cancelled', 'and True'),
            (paths[2], 'return not self.failed', 'return True'),
            (paths[2], 'os.waitpid(-1, os.WNOHANG)', 'os.waitpid(123, os.WNOHANG)'),
            (paths[2], 'parent != os.getpid()', 'False'),
            (paths[2], 'previous[0] != generation', 'False'),
            (paths[3], 'self._api.assign(self._job, self._process)', 'self._api.assign(None, self._process)'),
            (paths[3], 'if empty and exited:', 'if exited:'),
            (paths[4], 'acceptance_workflow_integration, check_process_owner_integration):',
                       'acceptance_workflow_integration):'))
        for path, old, new in mutants:
            with self.subTest(path=path, mutation=old):
                self.assertIn(old, sources[path])
                changed = {**sources, path: sources[path].replace(old, new)}
                self.assertTrue(violations(root, changed))
