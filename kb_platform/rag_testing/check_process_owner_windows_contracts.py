from __future__ import annotations

import ast
import io
from pathlib import Path
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import check_process_owner_windows as windows
from scripts import check_process_owner_witness as witness


class FakeAPI:
    def __init__(self, fail=None):
        self.events = []
        self.fail = fail
        self.active = 1
        self.exitcode = None
        self.duplicates = 0

    def event(self, name, *args):
        self.events.append((name, *args))
        if name == self.fail:
            raise OSError("sensitive command/environment text")

    def create_job(self):
        self.event("create_job")
        return 10

    def configure_job(self, job):
        self.event("configure_job", job)

    def duplicate_stream(self, stream):
        self.event("duplicate_stream")
        self.duplicates += 1
        return 20 + self.duplicates

    def audit(self, commandline, cwd, env):
        self.event("audit", "subprocess.Popen", None, commandline, cwd, env)

    def create_process(self, commandline, cwd, env, stdin_handle, output_handle):
        self.event("create_process", stdin_handle, output_handle)
        return 30, 31, 32, 33

    def assign(self, job, process):
        self.event("assign", job, process)

    def resume(self, thread):
        self.event("resume", thread)

    def terminate_job(self, job):
        self.event("terminate_job", job)

    def terminate_process(self, process):
        self.event("terminate_process", process)

    def active_processes(self, job):
        self.event("active_processes", job)
        return self.active

    def poll(self, process):
        self.event("poll", process)
        return self.exitcode

    def close(self, handle):
        self.event("close", handle)


class WindowsOwnerFakeContracts(unittest.TestCase):
    def owner(self, fail=None):
        api = FakeAPI(fail)
        with patch.object(windows, "_WindowsAPI", return_value=api):
            owner = windows.Owner()
        self.addCleanup(owner.close)
        return owner, api

    def spawn(self, owner):
        owner.spawn(["payload.exe", "two words"], "C:\\work", {"SECRET": "hidden"}, io.BytesIO())

    def test_native_create_uses_suspension_and_exact_handle_allowlist(self):
        api = object.__new__(windows._WindowsAPI)
        api.winapi = SimpleNamespace(STARTF_USESTDHANDLES=0x100, CreateProcess=Mock())
        with patch.object(windows.subprocess, "STARTUPINFO", SimpleNamespace, create=True):
            api.create_process("payload.exe", "C:\\work", {}, 21, 22)
        args = api.winapi.CreateProcess.call_args.args
        self.assertIsNone(args[0])
        self.assertTrue(args[4])
        self.assertEqual(args[5], 0x4)
        startup = args[8]
        self.assertEqual(startup.dwFlags, 0x100)
        self.assertEqual(startup.lpAttributeList, {"handle_list": [21, 22]})
        self.assertEqual((startup.hStdInput, startup.hStdOutput, startup.hStdError), (21, 22, 22))

    def test_assign_precedes_resume_and_native_thread_is_retained(self):
        owner, api = self.owner()
        self.spawn(owner)
        events = [event[0] for event in api.events]
        self.assertLess(events.index("create_process"), events.index("assign"))
        self.assertLess(events.index("assign"), events.index("resume"))
        self.assertIn(("create_process", 21, 22), api.events)
        self.assertIn(("resume", 31), api.events)
        self.assertIn(("close", 31), api.events)
        self.assertIn(("close", 21), api.events)
        self.assertIn(("close", 22), api.events)
        self.assertNotIn(("close", 30), api.events)

    def test_native_audit_boundary_preserves_exact_event_and_arguments(self):
        tree = ast.parse(Path(windows.__file__).read_text("utf-8"))
        native = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == "_WindowsAPI")
        method = next(node for node in native.body
                      if isinstance(node, ast.FunctionDef) and node.name == "audit")
        expected = ast.parse(
            'def audit(self, commandline, cwd, env):\n'
            '    sys.audit("subprocess.Popen", None, commandline, cwd, env)\n').body[0]
        self.assertEqual(ast.dump(method), ast.dump(expected))

    def test_audit_precedes_create_process(self):
        owner, api = self.owner()
        self.spawn(owner)
        events = [event[0] for event in api.events]
        self.assertLess(events.index("audit"), events.index("create_process"))
        audit = next(event for event in api.events if event[0] == "audit")
        self.assertEqual(audit[1:4], ("subprocess.Popen", None, 'payload.exe "two words"'))

    def test_audit_denial_never_creates_a_process(self):
        owner, api = self.owner("audit")
        with self.assertRaisesRegex(RuntimeError, "subprocess.Popen audit failed"):
            self.spawn(owner)
        self.assertFalse(any(event[0] == "create_process" for event in api.events))
        self.assertIn(("close", 10), api.events)
        self.assertIn(("close", 21), api.events)
        self.assertIn(("close", 22), api.events)

    def test_assignment_failure_terminates_exact_process_without_resume(self):
        owner, api = self.owner("assign")
        with self.assertRaisesRegex(RuntimeError, "assign suspended process to Job failed") as caught:
            self.spawn(owner)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertIn(("terminate_process", 30), api.events)
        self.assertFalse(any(event[0] == "resume" for event in api.events))
        for handle in (10, 21, 22, 30, 31):
            self.assertIn(("close", handle), api.events)

    def test_resume_failure_terminates_owned_process_and_job(self):
        owner, api = self.owner("resume")
        with self.assertRaisesRegex(RuntimeError, "resume owned process failed"):
            self.spawn(owner)
        self.assertIn(("terminate_process", 30), api.events)
        self.assertIn(("close", 10), api.events)

    def test_create_failure_closes_job_and_temporary_handles(self):
        owner, api = self.owner("create_process")
        with self.assertRaisesRegex(RuntimeError, "create suspended process failed"):
            self.spawn(owner)
        for handle in (10, 21, 22):
            self.assertIn(("close", handle), api.events)
        self.assertFalse(any(event[0] == "terminate_process" for event in api.events))

    def test_configuration_failure_closes_job(self):
        api = FakeAPI("configure_job")
        with patch.object(windows, "_WindowsAPI", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "configure and validate Job failed"):
                windows.Owner()
        self.assertIn(("close", 10), api.events)

    def test_cleanup_needs_both_job_empty_and_process_exited(self):
        owner, api = self.owner()
        self.spawn(owner)
        api.active = 0
        self.assertFalse(owner.cleanup(time.monotonic()))
        api.active = 1
        api.exitcode = 0
        self.assertFalse(owner.cleanup(time.monotonic()))
        api.active = 0
        self.assertTrue(owner.cleanup(time.monotonic()))
        self.assertEqual(owner.poll(), 0)
        self.assertEqual(sum(event[0] == "terminate_job" for event in api.events), 3)

    def test_normal_exit_still_terminates_descendants(self):
        owner, api = self.owner()
        self.spawn(owner)
        api.exitcode = 0
        self.assertEqual(owner.poll(), 0)
        api.active = 0
        self.assertTrue(owner.cleanup(time.monotonic()))
        self.assertIn(("terminate_job", 10), api.events)

    def test_close_is_idempotent_and_owner_cannot_be_reused(self):
        owner, api = self.owner()
        self.spawn(owner)
        owner.close()
        events = list(api.events)
        owner.close()
        self.assertEqual(events, api.events)
        with self.assertRaisesRegex(RuntimeError, "cannot be reused"):
            self.spawn(owner)


class WindowsWitnessFakeContracts(unittest.TestCase):
    def test_birth_mismatch_closes_without_termination(self):
        api = Mock()
        api.open.return_value = 123
        api.birth.return_value = 8
        with patch.object(witness, "_WitnessAPI", return_value=api):
            with self.assertRaisesRegex(RuntimeError, "creation time mismatch"):
                witness.OwnedWindowsWitness(99, 7)
        api.close.assert_called_once_with(123)
        api.terminate.assert_not_called()

    def test_retains_verified_handle_for_observation_and_termination(self):
        api = Mock()
        api.open.return_value = 123
        api.birth.return_value = 7
        api.running.return_value = True
        with patch.object(witness, "_WitnessAPI", return_value=api):
            owned = witness.OwnedWindowsWitness(99, 7)
        self.assertTrue(owned.running())
        owned.terminate()
        api.open.assert_called_once_with(99, terminate=True)
        api.birth.assert_called_once_with(123)
        api.terminate.assert_called_once_with(123)
        owned.close()
        owned.close()
        api.close.assert_called_once_with(123)

    def test_exited_process_is_not_terminated(self):
        api = Mock()
        api.open.return_value = 123
        api.birth.return_value = 7
        api.running.return_value = False
        with patch.object(witness, "_WitnessAPI", return_value=api):
            owned = witness.OwnedWindowsWitness(99, 7)
        owned.terminate()
        self.assertTrue(owned.wait(time.monotonic()))
        owned.close()
        api.terminate.assert_not_called()

    def test_fixture_receipt_only_describes_self(self):
        with patch.object(witness.os, "getpid", return_value=99):
            with self.assertRaisesRegex(ValueError, "own process"):
                witness.windows_birth(100)


if __name__ == "__main__":
    unittest.main()
