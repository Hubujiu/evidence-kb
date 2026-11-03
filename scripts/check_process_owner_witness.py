from __future__ import annotations

import ctypes
import os
import sys
import time


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _WitnessAPI:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Windows witness requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = ctypes.c_void_p
        dword = ctypes.c_uint32
        signatures = {
            "OpenProcess": ([dword, ctypes.c_int, dword], handle),
            "GetProcessTimes": ([handle] + [ctypes.POINTER(_FileTime)] * 4, ctypes.c_int),
            "WaitForSingleObject": ([handle, dword], dword),
            "TerminateProcess": ([handle, ctypes.c_uint], ctypes.c_int),
            "CloseHandle": ([handle], ctypes.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes = args
            function.restype = result

    @staticmethod
    def checked(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def open(self, pid, terminate=False):
        access = 0x100000 | 0x1000 | (0x1 if terminate else 0)
        return self.checked(self.kernel.OpenProcess(access, False, pid))

    def birth(self, handle):
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        self.checked(self.kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user)))
        return (created.high << 32) | created.low

    def running(self, handle):
        result = self.kernel.WaitForSingleObject(handle, 0)
        if result == 0x102:
            return True
        if result == 0:
            return False
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        raise RuntimeError("Unexpected witness wait result")

    def terminate(self, handle):
        self.checked(self.kernel.TerminateProcess(handle, 1))

    def close(self, handle):
        self.checked(self.kernel.CloseHandle(handle))


def windows_birth(pid: int) -> int:
    if pid != os.getpid():
        raise ValueError("Fixture birth receipt must describe its own process")
    api = _WitnessAPI()
    handle = api.open(pid)
    try:
        return api.birth(handle)
    finally:
        api.close(handle)


class OwnedWindowsWitness:
    def __init__(self, pid: int, expected_created: int):
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("Invalid fixture PID")
        if not isinstance(expected_created, int) or expected_created <= 0:
            raise ValueError("Invalid fixture creation time")
        self._api = _WitnessAPI()
        self._handle = self._api.open(pid, terminate=True)
        try:
            if self._api.birth(self._handle) != expected_created:
                raise RuntimeError("Fixture process creation time mismatch")
        except BaseException:
            self.close()
            raise

    def running(self) -> bool:
        if self._handle is None:
            raise RuntimeError("Fixture witness is closed")
        return self._api.running(self._handle)

    def terminate(self):
        if self.running():
            try:
                self._api.terminate(self._handle)
            except OSError:
                if self.running():
                    raise

    def wait(self, deadline: float) -> bool:
        while self.running():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.02, remaining))
        return True

    def close(self):
        if self._handle is not None:
            self._api.close(self._handle)
            self._handle = None


class OwnedLinuxWitness:
    def __init__(self, pid, expected_created):
        import signal
        from pathlib import Path
        self._signal = signal
        self._handle = os.pidfd_open(pid)
        try:
            generation = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
            if generation != str(expected_created):
                raise RuntimeError('WITNESS_IDENTITY_MISMATCH')
        except BaseException:
            os.close(self._handle)
            self._handle = None
            raise

    def running(self):
        import select
        poller = select.poll()
        poller.register(self._handle, select.POLLIN)
        return not poller.poll(0)

    def terminate(self):
        if self.running():
            self._signal.pidfd_send_signal(self._handle, self._signal.SIGKILL)

    def wait(self, deadline):
        while self.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.running()

    def close(self):
        if self._handle is not None:
            os.close(self._handle)
            self._handle = None


def own_birth():
    if sys.platform == 'win32':
        return windows_birth(os.getpid())
    from pathlib import Path
    return Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19]


def witness(pid, birth):
    if sys.platform == 'win32':
        return OwnedWindowsWitness(pid, birth)
    return OwnedLinuxWitness(pid, birth)
