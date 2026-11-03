# Verification process ownership: staged draft

This draft addresses a confirmed gap in `subprocess.run(timeout=...)`: the direct
child is killed, but a newly created descendant may remain alive. A tiny isolated
probe reproduced that gap. This is **not evidence that leftovers caused all prior
180-second host checks or 170-second contract-shard timeouts**.

## Stage one: helper and mandatory verification, not yet routed globally

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

## Compatibility changes when routing is enabled

- Linux needs an exclusive subreaper, pidfds and the kernel's own-task children
  interface. Missing capability causes refusal before starting the payload.
- The current local executor lacks `/proc/self/task/<self>/children`. Its observed
  capability rejection is a real blocker, not a successful process-tree test.
- Windows uses CPython 3.12/3.13's private `_winapi` adapter and native Job Objects.
  Suspended child creation precedes Job assignment, which precedes resumption.
  Native Windows results require the precise CI head, not Linux fake contracts.
- Darwin is explicitly unsupported for this ownership mode. It is not silently
  downgraded to process-group termination, which cannot contain setsid descendants.
  This is a verification-runner compatibility change requiring review, not a
  claim of universal platform support.

No existing unknown PID is adopted as owned. Linux only obtains handles for its
own supervisor children; a process-identity mismatch fails closed. The racy
children list is discovery only: ECHILD, not an empty list, confirms completion.
Windows uses native handles and confirms both an empty Job and direct-process exit.

SIGKILL/OOM/kernel failure of the Linux supervisor, hostile privilege changes,
unbounded process creation and inaccessible kernel ownership facilities are not
claimed recoverable. Cleanup failure remains explicit FAIL. No global process
scan, `pkill`, blanket process-group kill, new dependencies or system-wide setting
is used. Only the helper process itself acquires subreaper status.

## References and evidence

- [Python subprocess timeout and direct-child semantics](https://docs.python.org/3/library/subprocess.html#subprocess.run)
- [Linux child subreaper](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html)
- [Linux children-list race limits](https://man7.org/linux/man-pages/man5/proc_tid_children.5.html)
- Existing `rust/plugin-host/src/process_tree/` owner structure and its native
  Windows suspended/Job/resume sequence.
- [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

The formal model is a bounded safety abstraction, not an executable refinement or
an unlimited no-bug proof. Model traces and raw fixture logs remain private.
This work is a draft, has not been merged into main, and has not been deployed.

Initial local evidence: 23 pure contracts passed with the unchanged offline audit
seal active; the four real process cases are registered in foundation integration.
One actual local escaped-descendant case failed before payload creation because
owned-child discovery is unavailable. Its sentinel was cleaned through its own
Popen handle in finally. No local tree-cleanup PASS or full ordinary PASS is
claimed. The unchanged original runner remains in use only in this explicitly
incomplete first stage, not as a fallback of the new owner.
