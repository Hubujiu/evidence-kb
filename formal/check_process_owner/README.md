# Verification-check process ownership

This small finite safety model is for the verification gate's subprocess owner,
not the application plugin host. It has actual process state and operational
mutants, not a mutation flag used as the invariant's failure predicate.

## Operating-system contract and abstraction

- Linux: a **dedicated** helper is the subreaper. `Contain` represents enabling
  the subreaper before allowing the check command to execute (`Resume`). The
  model's common `suspended` setup state is an execution barrier, not a claim
  that Linux uses Windows creation flags. Only its children are captured; orphan
  grandchildren are adopted on direct-child exit. No process-group or session
  predicate is used. `EscapeSession` represents a grandchild calling `setsid`.
  The Linux [subreaper contract](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html)
  is the ancestry basis.
- Captured handles retain `(process identity, generation)`. `Signal` matches the
  handle generation, not just a recycled numeric PID. After a grandchild exits
  and is reaped, an unrelated process may reuse its PID while the old handle is
  retained. This models [pidfd signal identity](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html).
  Initial `sentinel` and later `reused` are outside the owner's process tree.
- `Reap` is distinct from death. Linux `ObserveEmpty` abstracts a confirmed
  ECHILD after the dedicated helper drains all adopted children, not WNOHANG
  returning zero or an empty `/proc` snapshot. See [wait semantics](https://man7.org/linux/man-pages/man2/waitpid.2.html).
- Windows: `Launch` creates suspended, `Contain` binds the Job, `Resume` permits
  execution, and `Fork` inherits Job membership. Breakaway is disabled. See
  [CREATE_SUSPENDED](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)
  and [Job inheritance and termination](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
  Handle-based termination is abstracted by `Signal`; Windows `ObserveEmpty`
  abstracts the active-process count becoming zero. The shared zombie/reap
  bookkeeping conservatively represents completion collection, not Windows
  POSIX wait calls.

`CleanupError` can occur with live descendants and leads to FAIL. There is no
assumed guarantee that termination succeeds. `Timeout` and `Cancel` are sticky
failure observations independent of the direct child's eventual exit status.
PASS requires exit zero, neither observation, confirmed cleanup, and no live or
uncollected owned descendant. Stuttering is permitted: these are safety checks,
not a proof that an unkillable child eventually terminates.

## Configurations

Two positive configurations, `CheckProcessOwner.cfg` and
`CheckProcessOwnerWindows.cfg`, check all seven invariants. Their finite bounds
are one direct child, one grandchild, one unrelated sentinel, one unrelated PID
reuse, and two generations.

| Negative config | Actual mutated behavior | Expected invariant |
| --- | --- | --- |
| DirectOnlySeeded | Signals only the direct child and accepts direct-only emptiness | CleanupCompleteMeansEmpty |
| EarlyResumeSeeded | Resumes without Job assignment | NoResumeBeforeContainment |
| StaleHandleSeeded | Uses matching PID even when handle generation is stale | NoPidReuseKill |
| UnownedKillSeeded | Same stale-handle fault signals an unrelated replacement | NoUnownedKill |
| OverwriteFailureSeeded | Replaces sticky failure with direct-child exit status at publication | PassRequiresEvidence |

`ReachPass`, `ReachPassWindows`, `ReachAdoptEscape`, `ReachCleanupError`,
`ReachReusedHandle`, `ReachTimeoutCleanup`, `ReachCancelledCleanup`,
`ReachEscapedCleanup`, and `ReachGenerationRefusal` are **coverage witnesses**, not safety negatives. Each checks
the negation of a reachable desired scenario and must produce the specifically
named invariant counterexample. They establish that PASS, escaped adoption,
cleanup error, and a retained handle after PID reuse are not unreachable states.
The stronger witnesses reach timeout/cancellation plus zero child exit and
completed cleanup with FAIL, actual termination of an escaped grandchild before
PASS, and refusal of a stale generation while the unrelated replacement remains
alive. `RefuseStale` abstracts the kernel rejecting an expired pidfd, not a
requirement to implement an unsafe userspace generation-check-then-kill race.
Coverage configs are registered using the existing expected-counterexample
control mechanism, with `coverage_` IDs; do not count them as bug mutants.

## Reproduce using the already-installed pinned TLC

From the repository root (no install/download/native build):

```sh
java -Xmx128m -cp /tmp/evidence-tla-tools/tla2tools-1.7.4.jar \
  tlc2.TLC -workers 1 -metadir /tmp/check-owner-review \
  -config formal/check_process_owner/CheckProcessOwner.cfg \
  formal/check_process_owner/CheckProcessOwner.tla
```

Use a fresh metadir per run/config. Positive checks require exit 0 and TLC's
completion message. Negatives and coverage require exit 12 **and the exact
expected invariant violation**, not an arbitrary nonzero exit. Registry integration uses model and expected-counterexample control entries.
Source bindings must be generated from the actual reviewed implementation, not
this specification. Raw TLC evidence is retained separately from source files.

## Scope and remaining obligations

This is not source refinement or an unbounded process-tree proof. It assumes a
single dedicated helper, no other child owner in that helper, successful
containment setup before execution, normal Windows CreateProcess inheritance
without breakaway, accurate OS handle semantics, and atomic abstract adoption.
It excludes helper crash/recovery, hostile new PID/user namespaces, privilege
changes, arbitrary multi-level trees, Windows WMI-created descendants, concurrent
new forks during teardown, and OS implementation bugs. Retained handles and
bounded PID reuse are included; actual races obtaining those handles and
confirmation that ECHILD is used correctly still require implementation review
and platform integration tests. Windows model checking on Linux is not execution
of Windows APIs. No full aggregate project gate or native build is claimed here.

Suggested source mapping: `supervise` and `Outcome.passed` correspond to
Timeout/Cancel/BeginCleanup/Publish; Linux `Owner.__init__`, `spawn`, `bind`,
`identity`, `poll`, `cleanup` correspond to containment/launch, identity capture,
exit collection, Signal/RefuseStale/Reap/ObserveEmpty; Windows `spawn`/`bind`/
`cleanup` correspond to suspended launch, Job assignment before Resume and Job
termination/empty observation. Review the final implementation before binding
these symbols. This mapping is not a claim that all implementation branches
are modeled.
