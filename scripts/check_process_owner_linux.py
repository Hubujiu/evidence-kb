from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time


class Owner:
    def __init__(self):
        self.process = None
        self.handles = {}
        self.failed = False
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise RuntimeError('PIDFD_CONTAINMENT_UNAVAILABLE')
        self.children = Path(f'/proc/self/task/{os.getpid()}/children')
        try:
            initial = self.children.read_text().strip()
        except OSError as exc:
            raise RuntimeError('OWNED_CHILD_DISCOVERY_UNAVAILABLE') from exc
        if initial:
            raise RuntimeError('SUPERVISOR_NOT_EXCLUSIVE')
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'SUBREAPER_SETUP_FAILED')

        handle = os.pidfd_open(os.getpid())
        os.close(handle)

    @staticmethod
    def identity(pid):
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return int(fields[1]), fields[19]

    def bind(self, pid):
        parent, generation = self.identity(pid)
        if parent != os.getpid():
            raise RuntimeError('NOT_SUPERVISOR_CHILD')
        handle = os.pidfd_open(pid)
        try:
            if self.identity(pid) != (parent, generation):
                raise RuntimeError('CHILD_IDENTITY_CHANGED')
            previous = self.handles.get(pid)
            if previous is not None:
                if previous[0] != generation:
                    raise RuntimeError('CHILD_PID_REUSED')
                os.close(handle)
                return previous[1]
            self.handles[pid] = generation, handle
            return handle
        except BaseException:
            os.close(handle)
            raise

    def spawn(self, command, cwd, env, stream):
        self.process = subprocess.Popen(command, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            close_fds=True)
        try:
            self.bind(self.process.pid)
        except (FileNotFoundError, ProcessLookupError):

            if self.process.poll() is None:
                raise

    def poll(self):
        return self.process.poll()

    def cleanup(self, deadline):
        while time.monotonic() < deadline:

            try:
                children = [int(value) for value in self.children.read_text().split()]
            except (OSError, ValueError):
                self.failed = True
                children = []
            for pid in children:
                try:
                    handle = self.bind(pid)
                    signal.pidfd_send_signal(handle, signal.SIGKILL)
                except (ProcessLookupError, FileNotFoundError):
                    pass
                except (OSError, RuntimeError):
                    self.failed = True
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return not self.failed
                except InterruptedError:
                    continue
                if pid == 0:
                    break
                if self.process is not None and pid == self.process.pid:
                    self.process.returncode = os.waitstatus_to_exitcode(status)


            time.sleep(0.01)
        return False

    def close(self):
        for _, handle in self.handles.values():
            os.close(handle)
        self.handles.clear()
