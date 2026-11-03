# Verification process ownership: current limits

This draft addresses a confirmed gap in `subprocess.run(timeout=...)`: the direct
child is killed, but a newly created descendant may remain alive. A tiny isolated
probe reproduced that gap. This is **not evidence that leftovers caused all prior
180-second host checks or 170-second contract-shard timeouts**.

## Current Linux-only scope

`run_check` routes checks through a dedicated Linux subreaper. Other platforms
are refused before payload creation with `UNSUPPORTED_PROCESS_CONTAINMENT` and
unconfirmed cleanup. Linux execution requires pidfds, signal support, and the
thread's `/proc` children interface; missing capabilities fail before payload
creation.

Ownership covers actual OS descendants and their adoption by the subreaper.
ECHILD does not observe work created by an independent external broker outside
that ancestry. A real WSL ext4 negative control demonstrated that boundary:
a previously running broker created a helper outside the tree, the payload
exited zero, and the owner reported PASS while that helper remained alive. The
broker subsequently terminated and waited for its own helper. This does not
show an OS descendant escaping.

Historical Windows tests and failures are retained as evidence, not current
platform support. A Store Python launch produced an OS descendant outside its
Job; strict Job accounting alone was insufficient. Windows is unsupported and
no Windows gate pass is claimed.

The bounded formal model is not source refinement or an unbounded tree proof.
The historical public combination manifest remains unchanged; the active
Linux-only manifest is `formal/combination-inputs-composition-v4.json` (the authority v3 and Linux v2 inventories remain historical).

## Historical stage one (superseded)

The helper is exercised by actual process fixtures in the existing mandatory
foundation integration stage. Pure adapter, failure, identity and lint contracts
stay in sealed offline workers without loosening their audit hook. This first
stage does not change `run_check` execution routing and does not claim the original
gap is fixed for all gates. A subsequent update to this same draft will enable the
helper for all gates after real Linux/Windows fixture evidence and review.

Execution deadlines remain separate from cleanup deadlines. Once timeout or
cancellation is observed it remains FAIL, including when cleanup succeeds. Normal
child exit still requires descendant cleanup. The result records cleanup duration.
The helper launches before native compilation and uses no built native library.

## Historical first-stage compatibility observations (superseded)

The following observations describe the earlier prototype and its execution
environment. The current implementation and supported platform are described
in the Linux-only scope above.

- Linux needs an exclusive subreaper, pidfds and the kernel's own-task children
  interface. Missing capability causes refusal before starting the payload.
- The current local executor lacks `/proc/self/task/<self>/children`. Its observed
  capability rejection is a real blocker, not a successful process-tree test.
- The Windows prototype used CPython 3.12/3.13's private `_winapi` adapter and
  native Job Objects. Suspended child creation preceded Job assignment and
  resumption. That adapter has been removed.
- Darwin is explicitly unsupported for this ownership mode. It is not silently
  downgraded to process-group termination, which cannot contain setsid descendants.
  This is a verification-runner compatibility change requiring review, not a
  claim of universal platform support.

No existing unknown PID is adopted as owned. Linux only obtains handles for its
own supervisor children; a process-identity mismatch fails closed. The racy
children list is discovery only: ECHILD, not an empty list, confirms completion.
The experimental Windows adapter observed an empty Job and direct-process exit;
those observations did not prove all delegated work exited.

SIGKILL/OOM/kernel failure of the Linux supervisor, hostile privilege changes,
unbounded process creation and inaccessible kernel ownership facilities are not
claimed recoverable. Cleanup failure remains explicit FAIL. No global process
scan, `pkill`, blanket process-group kill, new dependencies or system-wide setting
is used. Only the helper process itself acquires subreaper status.

## References and evidence

- [Python subprocess timeout and direct-child semantics](https://docs.python.org/3/library/subprocess.html#subprocess.run)
- [Linux child subreaper](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html)
- [Linux children-list race limits](https://man7.org/linux/man-pages/man5/proc_tid_children.5.html)
- The historical `rust/plugin-host/src/process_tree/` Windows
  suspended/Job/resume sequence, removed from the Linux-only implementation.
- [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

The formal model is a bounded safety abstraction, not an executable refinement or
an unlimited no-bug proof. Model traces and raw fixture logs remain private.
The following initial evidence describes the earlier draft, before Linux-only
routing replaced the original runner.

Initial local evidence: 23 pure contracts passed with the unchanged offline audit
seal active; the four real process cases are registered in foundation integration.
One actual local escaped-descendant case failed before payload creation because
owned-child discovery is unavailable. Its sentinel was cleaned through its own
Popen handle in finally. No local tree-cleanup PASS or full ordinary PASS is
claimed. The unchanged original runner remains in use only in this explicitly
incomplete first stage, not as a fallback of the new owner.

## Supervisor watchdog boundary

A pre-cancelled request does not start a supervisor or payload. If a supervisor does not exit within the bounded wait following cancellation or watchdog expiry, the caller records `OWNER_SUPERVISOR_WAIT_TIMEOUT` and unconfirmed cleanup. A background waiter retains the temporary result directory until that supervisor exits. Linux cannot safely force-kill the supervisor without losing its subreaper ownership of descendants, so this failure does not guarantee a bounded process-tree shutdown. The direct child's exit time is the monotonic time at which polling observed termination, an upper bound rather than its exact exit time. Cleanup finish records the completion of the cleanup attempt; `cleanup_complete` independently states whether emptiness was confirmed. Missing observations remain absent.

If the caller raises during supervision, the original exception is preserved. After signalling cancellation, an exception note reports unconfirmed cleanup when the owner result is missing, indicates failure, or does not arrive before the wait deadline. A timed-out supervisor is still reaped asynchronously if it later exits.

The supervisor receives bounded startup metadata through a reserved, temporary environment variable; the payload environment does not contain that variable. The original environment is not serialized into the control channel or written to a file. Oversized metadata and a reserved-name collision fail before spawning. Temporary references are discarded after startup, but Python does not guarantee secure memory erasure. The cancellation pipe carries no configuration and is closed to signal cancellation. If an empty process tree is observed only after the cleanup deadline, the result retains `cleanup_complete=true` but fails with `CLEANUP_DEADLINE_EXCEEDED`. OS process creation and supervisor crashes remain separate limitations.

## Contract timing and watchdog diagnostics

The ordinary contract worker retains per-case wall and CPU timing. At 150 seconds,
it attempts to record each Python thread's file, function and line without local
variables or arguments. This diagnostic thread needs the GIL: native code holding
the GIL can delay or prevent the snapshot. The outer deadlines and failure
semantics still apply; a missing or incomplete snapshot after 150 seconds, or a
watchdog thread that cannot be confirmed stopped, remains a failure. Passing the
diagnostic threshold alone does not prove a hang. The watchdog is cancelled and
waited for at most one second during worker shutdown.
