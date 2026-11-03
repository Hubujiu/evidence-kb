from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

_DWORD = ctypes.c_uint32
_SIZE = ctypes.c_size_t
_HANDLE = ctypes.c_void_p
_KILL_ON_JOB_CLOSE = 0x2000
_CREATE_SUSPENDED = 0x4


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE),
        ("MaximumWorkingSetSize", _SIZE),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE),
        ("JobMemoryLimit", _SIZE),
        ("PeakProcessMemoryUsed", _SIZE),
        ("PeakJobMemoryUsed", _SIZE),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


class _WindowsAPI:

    def __init__(self):
        if sys.platform != "win32" or sys.implementation.name != "cpython":
            raise RuntimeError("Windows process ownership requires CPython on Windows")
        import _winapi
        import msvcrt

        self.winapi = _winapi
        self.msvcrt = msvcrt
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([_HANDLE, ctypes.c_wchar_p], _HANDLE),
            "SetInformationJobObject": ([_HANDLE, ctypes.c_int, _HANDLE, _DWORD], ctypes.c_int),
            "QueryInformationJobObject": (
                [_HANDLE, ctypes.c_int, _HANDLE, _DWORD, _HANDLE], ctypes.c_int),
            "AssignProcessToJobObject": ([_HANDLE, _HANDLE], ctypes.c_int),
            "TerminateJobObject": ([_HANDLE, ctypes.c_uint], ctypes.c_int),
            "ResumeThread": ([_HANDLE], _DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes = args
            function.restype = result

    @staticmethod
    def checked(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def create_job(self):
        return self.checked(self.kernel.CreateJobObjectW(None, None))

    def configure_job(self, job):
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        self.checked(self.kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        actual = _ExtendedLimits()
        self.checked(self.kernel.QueryInformationJobObject(
            job, 9, ctypes.byref(actual), ctypes.sizeof(actual), None))
        if actual.BasicLimitInformation.LimitFlags != _KILL_ON_JOB_CLOSE:
            raise RuntimeError("Job limits did not match requested ownership")

    def duplicate_stream(self, stream):
        current = self.winapi.GetCurrentProcess()
        return self.winapi.DuplicateHandle(
            current, self.msvcrt.get_osfhandle(stream.fileno()), current,
            0, True, self.winapi.DUPLICATE_SAME_ACCESS)

    def audit(self, commandline, cwd, env):
        sys.audit("subprocess.Popen", None, commandline, cwd, env)

    def create_process(self, commandline, cwd, env, stdin_handle, output_handle):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = self.winapi.STARTF_USESTDHANDLES
        startup.hStdInput = stdin_handle
        startup.hStdOutput = output_handle
        startup.hStdError = output_handle
        startup.lpAttributeList = {"handle_list": [stdin_handle, output_handle]}
        return self.winapi.CreateProcess(
            None, commandline, None, None, True, _CREATE_SUSPENDED,
            env, cwd, startup)

    def assign(self, job, process):
        self.checked(self.kernel.AssignProcessToJobObject(job, process))

    def resume(self, thread):
        count = self.kernel.ResumeThread(thread)
        if count == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if count != 1:
            raise RuntimeError("Initial thread suspend count changed")

    def terminate_job(self, job):
        self.checked(self.kernel.TerminateJobObject(job, 1))

    def terminate_process(self, process):
        self.winapi.TerminateProcess(process, 1)

    def active_processes(self, job):
        accounting = _Accounting()
        self.checked(self.kernel.QueryInformationJobObject(
            job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return accounting.ActiveProcesses

    def poll(self, process):
        result = self.winapi.WaitForSingleObject(process, 0)
        if result == self.winapi.WAIT_OBJECT_0:
            return self.winapi.GetExitCodeProcess(process)
        if result != self.winapi.WAIT_TIMEOUT:
            raise RuntimeError("Unexpected process wait result")
        return None

    def close(self, handle):
        self.winapi.CloseHandle(handle)


def _failure(stage, error):
    code = getattr(error, "winerror", None)
    suffix = "" if code is None else f" (WinError {code})"
    return RuntimeError(f"Windows process owner: {stage} failed{suffix}")


class Owner:
    def __init__(self):
        self._job = self._process = self._thread = None
        self._started = False
        self.pid = None
        self.returncode = None
        stage = "load native API"
        try:
            self._api = _WindowsAPI()
            stage = "create Job"
            self._job = self._api.create_job()
            stage = "configure and validate Job"
            self._api.configure_job(self._job)
        except BaseException as error:
            self.close()
            if not isinstance(error, Exception):
                raise
            raise _failure(stage, error) from None

    def spawn(self, command: list[str], cwd: str, env: dict[str, str], stream):
        if self._started or self._job is None:
            raise RuntimeError("Windows process owner cannot be reused")
        self._started = True
        inherited = []
        stage = "prepare standard handles"
        try:
            with open(os.devnull, "rb") as devnull:
                inherited.append(self._api.duplicate_stream(devnull))
            inherited.append(self._api.duplicate_stream(stream))
            stage = "encode command line"
            commandline = subprocess.list2cmdline(command)
            stage = "subprocess.Popen audit"
            self._api.audit(commandline, cwd, env)
            stage = "create suspended process"
            self._process, self._thread, self.pid, _ = self._api.create_process(
                commandline, cwd, env, inherited[0], inherited[1])
            stage = "assign suspended process to Job"
            self._api.assign(self._job, self._process)
            stage = "resume owned process"
            self._api.resume(self._thread)
            stage = "close initial thread handle"
            self._api.close(self._thread)
            self._thread = None
        except BaseException as error:
            abort_failed = False
            if self._process is not None:
                try:
                    self._api.terminate_process(self._process)
                except Exception:
                    try:
                        abort_failed = self._api.poll(self._process) is None
                    except Exception:
                        abort_failed = True
            try:
                self.close()
            except Exception:
                abort_failed = True
            if not isinstance(error, Exception):
                raise
            if abort_failed:
                stage += "; abort could not be confirmed"
            raise _failure(stage, error) from None
        finally:
            failures = []
            for handle in inherited:
                try:
                    self._api.close(handle)
                except Exception as error:
                    failures.append(error)
            if failures:
                self.close()
                raise _failure("close inherited handle copies", failures[0]) from None

    def poll(self) -> int | None:
        if self._process is None:
            return self.returncode
        try:
            if self.returncode is None:
                self.returncode = self._api.poll(self._process)
            return self.returncode
        except Exception as error:
            raise _failure("poll process handle", error) from None

    def cleanup(self, deadline: float) -> bool:
        if self._job is None:
            return not self._started
        stage = "terminate Job"
        try:
            self._api.terminate_job(self._job)
            while True:
                stage = "query Job active process count"
                empty = self._api.active_processes(self._job) == 0
                stage = "wait for owned process exit"
                exited = self._process is None or self.poll() is not None
                if empty and exited:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.02, remaining))
        except Exception as error:
            raise _failure(stage, error) from None

    def close(self):
        failures = []
        for name in ("_job", "_thread", "_process"):
            handle = getattr(self, name, None)
            if handle is not None:
                try:
                    self._api.close(handle)
                except Exception as error:
                    failures.append(error)
                else:
                    setattr(self, name, None)
        if failures:
            raise _failure("close owned handles", failures[0]) from None
