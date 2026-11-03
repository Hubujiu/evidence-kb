# Verification process ownership on Windows

The Windows verification owner uses only the standard library on CPython. It calls `_winapi.CreateProcess` directly to retain both the process and initial-thread native handles. This is a narrow process launcher, not a copy of `subprocess.Popen`.

The launcher duplicates DEVNULL and the caller's log stream as inheritable native handles, explicitly allowlists those two handles in `STARTUPINFO.lpAttributeList`, and redirects both output streams to the log. It does not change ownership of the caller's stream. The unnamed Job handle and the returned process/thread handles are not passed to the child.

Before creating a process, the launcher emits the same `subprocess.Popen` audit event and arguments as CPython's Windows implementation. Native `_winapi` auditing remains in place. An audit denial prevents creation. Errors contain stage names and OS error numbers, never command arguments, environment values, or exception text that could contain secrets.

The process starts with `CREATE_SUSPENDED`. Its retained native process handle is assigned to a Job configured and queried to verify exactly `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, with no breakaway permission. Only successful assignment permits resuming the retained initial-thread handle. There is no PID or system-wide thread enumeration. Assignment failure terminates only the process represented by the owned native handle and closes all owned handles without resuming it.

Cleanup terminates the Job even if the payload parent exited successfully. It confirms both zero active Job processes and a signaled parent process handle before reporting success, bounded by the caller's monotonic deadline. Closing the Job provides a final kill guard, but close alone is not a substitute for confirmed cleanup.

Windows 8 and later support nested Jobs. An incompatible enclosing Job or denied assignment fails closed rather than using a breakaway process. The project requires Python 3.12 or newer; the private `_winapi` interface is a CPython dependency and must be retested when upgrading Python. No support for other Python implementations is claimed.

## Verification limits

`kb_platform/rag_testing/check_process_owner_windows_contracts.py` uses a pure fake native API. Its tests cover assignment-before-resume, retained handles, the exact inheritance allowlist and creation flags, audit ordering/denial, create/assignment/resume failures, handle closure, normal-exit descendant termination, and cleanup's dual conditions. These tests start no real processes and do not prove native ABI compatibility or Windows kernel behavior. The fake native boundary records audit attempts locally without monkeypatching global auditing. An exact AST contract verifies that the real native boundary emits the unchanged `subprocess.Popen` audit event and arguments. All 16 contracts pass with `seal_offline_worker()` active.

Real Windows execution was unavailable in the Linux implementation environment. Windows parent/child/grandchild cleanup and native-handle behavior remain unverified until the Windows CI run executes the real integration tests. A fake-only pass must not be reported as Windows validation.

## Sources

- [CPython 3.12 subprocess implementation](https://github.com/python/cpython/blob/3.12/Lib/subprocess.py): `_execute_child`, `STARTUPINFO`, handle inheritance, audit event, and `_winapi.CreateProcess`.
- [Microsoft Job Objects](https://learn.microsoft.com/windows/win32/procthread/job-objects): descendant membership, nested Jobs, breakaway permissions, termination, and kill-on-close.
- [SetInformationJobObject](https://learn.microsoft.com/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject): extended Job limits.
- [UpdateProcThreadAttribute](https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute): explicit inheritable-handle list and `bInheritHandles` requirements.
- [ResumeThread](https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread): initial suspend-count validation.

The repository's `rust/plugin-host/src/process_tree/windows.rs` supplies the existing Job configuration and active-process accounting semantics. Unlike that implementation, this launcher already holds the original thread handle and therefore does not discover threads through a system snapshot.

## Windows integration-test witnesses

`scripts/check_process_owner_witness.py` exposes `windows_birth(pid)` for the fixture to record its own exact `GetProcessTimes` creation timestamp (an integer in native FILETIME units). It rejects requests to create receipts for another process.

The test parent can construct `OwnedWindowsWitness(pid, expected_created)` from that fixture receipt. It opens a non-inheritable handle with only synchronize, query-limited-information, and terminate rights, then validates creation time on that same handle. A mismatched birth closes the handle without termination. `running()` inspects the retained handle, `terminate()` targets only that handle, `wait(deadline)` waits to a monotonic deadline, and `close()` releases it. No method reacquires a PID or enumerates processes. The caller must acquire the witness while the fixture is alive, keep it through cleanup, and close it afterward. Permission failures and missing processes remain visible rather than being treated as successful cleanup.
