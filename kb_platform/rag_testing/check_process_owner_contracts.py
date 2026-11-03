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

    def test_non_linux_platform_refuses_both_entrypoints_before_spawn(self):
        from threading import Event
        for platform in ('win32', 'darwin'):
            for cancelled in (False, True):
                event = Event()
                if cancelled:
                    event.set()
                with self.subTest(platform=platform, cancelled=cancelled), \
                        patch('scripts.check_process_owner.sys.platform', platform), \
                        patch('scripts.check_process_owner.subprocess.Popen') as spawn, \
                        patch('scripts.check_process_owner.backend') as backend:
                    caller = run_owned(['payload'], cwd='.', env={}, stream=None, timeout=1,
                                       cancel_event=event)
                    supervisor = supervise({}, event)
                    spawn.assert_not_called()
                    backend.assert_not_called()
                    for result in (caller, supervisor):
                        self.assertEqual('UNSUPPORTED_PROCESS_CONTAINMENT', result.error)
                        self.assertEqual(cancelled, result.cancelled)
                        self.assertFalse(result.cleanup_complete)
                        self.assertFalse(result.passed)
            from scripts.check_process_owner import backend as actual_backend
            with patch('scripts.check_process_owner.sys.platform', platform):
                with self.assertRaisesRegex(RuntimeError, 'UNSUPPORTED_PROCESS_CONTAINMENT'):
                    actual_backend()

    def test_pre_cancelled_work_does_not_launch_a_supervisor(self):
        from threading import Event
        cancelled = Event()
        cancelled.set()
        with patch('scripts.check_process_owner.subprocess.Popen') as spawn:
            result = run_owned([], cwd='.', env={}, stream=None, timeout=1, cancel_event=cancelled)
        spawn.assert_not_called()
        self.assertTrue(result.cancelled)
        self.assertFalse(result.passed)

    @patch('scripts.check_process_owner.sys.platform', 'linux')
    def test_supervisor_wait_timeout_reports_unconfirmed_cleanup(self):
        import io
        import subprocess
        from unittest.mock import MagicMock

        process = MagicMock()
        process.stdin = io.StringIO()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired('owner', 1)
        cancellation = MagicMock()
        cancellation.is_set.side_effect = [False, True]
        with patch('scripts.check_process_owner.subprocess.Popen', return_value=process), \
                patch('scripts.check_process_owner.threading.Thread') as thread:
            result = run_owned([], cwd='.', env={}, stream=None, timeout=1,
                               cleanup_timeout=1, cancel_event=cancellation)
        self.assertEqual('OWNER_SUPERVISOR_WAIT_TIMEOUT', result.error)
        self.assertFalse(result.cleanup_complete)
        self.assertFalse(result.passed)
        thread.return_value.start.assert_called_once()
        thread.call_args.kwargs['args'][1].cleanup()

    @patch('scripts.check_process_owner.sys.platform', 'linux')
    def test_caller_interrupt_reports_unconfirmed_cleanup(self):
        import io
        from unittest.mock import MagicMock

        process = MagicMock()
        process.stdin = io.StringIO()
        process.poll.return_value = None
        cancellation = MagicMock()
        cancellation.is_set.side_effect = [False, KeyboardInterrupt()]
        with patch('scripts.check_process_owner.subprocess.Popen', return_value=process):
            with self.assertRaises(KeyboardInterrupt) as caught:
                run_owned([], cwd='.', env={}, stream=None, timeout=1, cancel_event=cancellation)
        self.assertIn('OWNER_INTERRUPTED: cleanup unconfirmed', caught.exception.__notes__)
        self.assertTrue(process.stdin.closed)

    @patch('scripts.check_process_owner.sys.platform', 'linux')
    def test_oversized_or_conflicting_control_environment_never_spawns(self):
        from scripts.check_process_owner import _CONFIG_ENV
        with patch('scripts.check_process_owner.subprocess.Popen') as spawn:
            conflict = run_owned([], cwd='.', env={_CONFIG_ENV.lower(): 'secret'}, stream=None, timeout=1)
            oversized = run_owned(['x' * 25000], cwd='.', env={}, stream=None, timeout=1)
        spawn.assert_not_called()
        self.assertEqual('OWNER_CONFIG_ENV_CONFLICT', conflict.error)
        self.assertEqual('OWNER_CONFIG_TOO_LARGE', oversized.error)
        self.assertFalse(conflict.passed)
        self.assertFalse(oversized.passed)

    @patch('scripts.check_process_owner.sys.platform', 'linux')
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
        with patch('scripts.check_process_owner_linux.os.WNOHANG', 1, create=True), \
                patch('scripts.check_process_owner_linux.os.waitpid', return_value=(0, 0)), \
                patch('scripts.check_process_owner_linux.time.monotonic', side_effect=[0, 2]), \
                patch('scripts.check_process_owner_linux.time.sleep'):
            self.assertFalse(owner.cleanup(1))
        with patch('scripts.check_process_owner_linux.os.WNOHANG', 1, create=True), \
                patch('scripts.check_process_owner_linux.os.waitpid', side_effect=ChildProcessError), \
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
                 'scripts/check_process_owner_linux.py',
                 'kb_platform/rag_testing/foundation_integration.py')
        sources = {path: (root / path).read_text('utf-8') for path in paths}
        self.assertEqual([], violations(root, sources))
        mutants = ((paths[0], 'violations.extend(process_owner_violations(ROOT))', 'pass'),
            (paths[0], 'from scripts.check_process_owner_lint import violations as process_owner_violations', 'pass'),
            (paths[0], 'result = run_owned(command, cwd=ROOT, env=env, stream=stream, timeout=timeout)',
                    'result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, timeout=timeout)'),
            (paths[0], 'code = 0 if result.passed else result.exit_code or 2', 'code = 0'),
            (paths[1], 'config_text = os.environ.pop(_CONFIG_ENV)', 'config_text = os.environ[_CONFIG_ENV]'),
            (paths[1], "if sys.platform != 'linux':\n        return Outcome(None, False, cancelled.is_set(), False, 0,",
                       "if False:\n        return Outcome(None, False, cancelled.is_set(), False, 0,"),
            (paths[1], "if sys.platform != 'linux':\n        return Outcome(None, False, bool(cancel_event and cancel_event.is_set()), False, 0,",
                       "if False:\n        return Outcome(None, False, bool(cancel_event and cancel_event.is_set()), False, 0,"),
            (paths[1], "error = error or 'CLEANUP_DEADLINE_EXCEEDED'", 'pass'),
            (paths[1], 'supervisor_env.pop(_CONFIG_ENV, None)', 'pass'),
            (paths[1], 'caller_cancelled = False', "process.stdin.write('x')\n        caller_cancelled = False"),
            (paths[1], 'and self.cleanup_complete', 'and True'),
            (paths[1], 'and not self.timed_out', 'and True'),
            (paths[1], 'and not self.cancelled', 'and True'),
            (paths[2], 'return not self.failed', 'return True'),
            (paths[2], 'os.waitpid(-1, os.WNOHANG)', 'os.waitpid(123, os.WNOHANG)'),
            (paths[2], 'parent != os.getpid()', 'False'),
            (paths[2], 'previous[0] != generation', 'False'),
            (paths[3], 'acceptance_workflow_integration, check_process_owner_integration):',
                       'acceptance_workflow_integration):'))
        for path, old, new in mutants:
            with self.subTest(path=path, mutation=old):
                self.assertIn(old, sources[path])
                changed = {**sources, path: sources[path].replace(old, new)}
                self.assertTrue(violations(root, changed))
