# Host/process ownership and observer knowledge (bounded)

Baseline: `c8ef7582f226c07f1c06d3874dba159708ca7fad`. This batch changes
models, regression fixtures and mandatory semantic guards, **not production
owners**. No current production defect is asserted. All five model negative
controls are explicitly seeded violations, not reproductions of existing bugs.

## Mature references reviewed first

- [Erlang supervisor principles](https://www.erlang.org/doc/system/sup_princ.html),
  reviewed 2026-11-02: supervision owns child start/stop/monitoring; shutdown
  distinguishes asking a child to stop, waiting for its exit, and hard
  termination after a deadline. This informs ownership boundaries, not a claim
  that our OS process handling is Erlang-equivalent.
- [Temporal Activity definition / idempotency](https://docs.temporal.io/activity-definition),
  reviewed 2026-11-02: successful effects can precede a lost completion report;
  backend-enforced deduplication is distinct from a request identifier. Unlike
  Temporal's configured retry mechanism, this transport does **not** silently
  retry UNKNOWN work. A fresh UUID does not make replay safe.
- Existing pinned DSH/Cordis lifecycle review at DSH `ce7073d`:
  vendored Cordis fiber blob `6da19b4767c79a9f8a019e701fa852a0b07d4072`,
  SHA-256 `b64c3a97f15f0381479dcf3d25965e56dc4cecfa4a0881fc055a49f65b9945df`.
  Completion/cleanup ownership is intentionally separate from consumer-facing
  terminal knowledge, consistent with that earlier lifecycle review.

## Distinct protocols and source-to-model mapping

The two transports below have different envelopes, ID namespaces and owners.
Combining them into a fictional generic transport would conceal obligations.

### HostSupervisorLifecycle

Bound: one app, one run, one shutdown, one root; finite close and supervisor
cleanup outcomes. No fairness assumption makes arbitrary app code terminate.

| Model actions / facts | Actual source owner |
| --- | --- |
| Boot, StartRun, ClaimStop | `ApplicationWorker._claim`, `accept`, `_execute`; `Host::run` handshake/boot/run/shutdown exchanges |
| RunReturns, BeginClose, CloseReturns | `_run` finally: sets finishing, waits `_stop_done`, then calls app.close |
| StopDone, SettleRun, shutdown reply | `_shutdown`: clears/sets stop completion in finally and waits `_settled`; `_execute` marks settlement after actual run/close and attempted reply |
| EmitRunReply vs RunAcknowledged | attempted worker reply and parent-observed lifecycle response are separate facts; a lost reply need not prevent real settlement |
| CollectRoot | `next_response` records native create/collect success, not speculative root removal |
| Reap, Cleanup, Publish | `Worker::reap`, `ProcessTree::shutdown`, final `(outcome, cleanup)` match in `Host::run` |
| Fail / absent acknowledgement | `Worker::event`, `receive_messages`; timeout/EOF/failure do not publish success |

The model isolates the running-app cleanup path. Startup error cleanup, ready
without run, multiple roots, and all message loss schedules remain obligations;
no exhaustive source refinement is claimed. `reaped` is the supervisor's leader
observation, not a promise that arbitrary descendants have disappeared.

**Platform boundary:** Unix `Group::is_empty()` returns `true`; its cleanup
result cannot prove instantaneous physical disappearance of every descendant.
The Unix binding includes `killpg`, non-reaping `waitid` status, and the actual
`is_empty` implementation. Windows bindings include Job Object termination and
`ActiveProcesses == 0`. The model's `cleanup = ack` is only the implementation's
acknowledgement; existing platform integration remains separate. No Windows
execution is claimed from local Linux results.

Seeded profiles: omit the stop wait (`NoConcurrentStopClose`), or ignore final
supervisor cleanup error (`SuccessRequiresCleanup`). Both must exit 12 with the
exact named invariant trace.

### HostRequestCorrelation

Bound: one connection, two unique allocated requests, arbitrary send/dispatch/
reply order. `Allocate` maps to `HostConnection.request`'s locked ID/future
installation; `Send` has a separate `_write_lock`; `Receive` maps exact response
ID to a pending future. `Fail` rejects pending/new requests, while a previously
written native request can already have been dispatched by Rust `next_response`
and `dispatch_call`. No automatic replay action exists in the positive model.

Allocation order is **not wire order**. `[2, 1]` is legal and covered by an actual
Event-controlled test. `ApplicationWorker.accept`'s monotonic lifecycle IDs are
the opposite protocol direction and are not imposed on native requests. A
malicious worker sending duplicate native IDs is outside this trusted-client
bound; lack of a native replay cache is not called a production vulnerability.
The seeded profile intentionally settles a different future (`ExactCorrelation`).

### ProviderPhysicalKnowledge

Bound: one logical operation, one worker, two physical IDs (the second exists
only in the seeded replay profile). This is `evidence-plugin-process/1`, with
string UUIDs, not the numeric native-call namespace.

| Model fact/action | Actual source owner |
| --- | --- |
| prepared / ClaimIntent | `ProcessTransport.request`, `_enqueue`, `_Write`; `_write` sets sent intent **before** journal and bytes |
| WriteFrame / Invoke / Return | actual pipe write and SDK `serve` handler dispatch/return; invocation count is not affected rows or bytes |
| Observe / Lose | `_read`, request's `write.sent` error classifier, UNKNOWN record and termination; a missing result permits zero or one actual invocation |
| Cancel | `_watch_writes`, cancellation control and SDK `CallContext::check`; cancellation is not handler completion |
| Join / ClosePlugin | SDK cancels active calls, joins actual worker handles, then calls plugin.close, even if result writing failed |
| Exit / StopReader / StopWriter / CloseTransport | Python `_terminate`, `_stop_writer`, close's reader/supervisor joins before `_closed = True` |

Actual provider task termination and client I/O cleanup are different owners.
Forced process termination may close the transport without a cooperative plugin
close acknowledgement. UNKNOWN work may have been written before failure and
invoked before physical exit; sent intent alone still admits zero invocations.
The model does not equate process kill request with effect rollback or plugin
cleanup. The bounded receipt abstraction merges observed provider success/error
as known; it does not assert success changed any business data.

SDK `RequestIds` retains 1,024 recent IDs plus a separate active-call guard.
Existing Rust tests deliberately allow an evicted ID again. This finite model
does not exercise eviction or assert durable idempotency. The replay negative
uses a **new physical ID for the same logical operation**, not a recent duplicate
that the SDK correctly rejects. Negative profiles target `NotSentMeansNoInvocation`
and `NoDuplicateInvocation` respectively; the latter trace reaches two actual
handler invocations for the same logical operation, not merely a second prepared
request. The positive model also checks `NoReplayAfterUnknown` (no second attempt).

## Concrete regressions and mandatory entry points

- `host_owner_contracts.py`: real HostConnection with Event-controlled `[2,1]`
  writes and exact replies; duplicate reply and post-failure request rejection.
  Real ApplicationWorker with Event-controlled stop and close: stop end precedes
  close start; no shutdown reply while close remains blocked; run reply precedes
  shutdown reply. No sleeps establish causality.
- `host_process_integration.verify_physical_knowledge` is called by the existing
  ordinary `plugin-process-integration` gate, not by sealed contract workers.
  Two real local children: one fsyncs an effect then exits 7 without a reply;
  the other sees no business bytes because SENT journal recording is faulted
  after intent. Both return exactly `PROCESS_RESULT_UNKNOWN`, keep one physical
  request, never replay, and stop actual child/reader/writer/deadline threads.
  Exact journals are PREPARED/SENT/UNKNOWN and PREPARED/UNKNOWN respectively.
- Rust `lost_successful_response_joins_and_closes_without_reinvoking`: real SDK
  serves an invocation then writer reports BrokenPipe on that successful result.
  Handler finishes once, plugin closes once after join, serve returns the error;
  no successful result frame and no reinvocation. Channel gating, no sleeps.
- `host_process_lint.py` is invoked by mandatory `registry_violations`, hence
  ordinary formal-lint **and** formal-only, not merely a manual contract. AST/
  structure guards cover Python stop/settle, correlation, send intent/UNKNOWN,
  close joins; Rust host outcome reconciliation, SDK join/close and bounded ID
  checks; frame limits and platform process-tree observations.
- Discovered structural contracts reject 12 seeded mutations and demonstrate
  rejection even after refreshing source-binding hashes. A separate discovered
  AST contract ensures the real-process regression remains on the ordinary
  integration path. Structural helpers are not complete language parsers or
  machine-checked refinement proofs.

## Evidence / validation boundaries

`evidence/` preserves targeted actual logs and their exit codes. Original model
logs precede review refinements: supervisor now separates reply publication from
real settlement, and physical knowledge now permits a queued request to execute
after knowledge loss but before process exit. Files with `-final` are the revised
profiles. The incomplete-response model diagnostic (exit 75) is also retained: a newly
introduced observation variable was initially omitted from some UNCHANGED
clauses, then corrected before the final successful TLC runs.
Initial structural test failure is retained: two string mutants first
hit the separate callback method rather than `request`; they were scoped to the
reviewed request owner, then all 24 structural/owner/infra cases passed.

Targeted Rust crate: 17 passed; real physical-knowledge controls: 2 passed;
Python owner/structural/infra: 24 passed; Ruff and formal lint pass. The additional
formal-only and ordinary unified gates must run on the frozen committed tree;
their external unique directories and true outcomes are reported separately.
A known host/SOCKS environmental failure is not replaced by targeted success.
Zero new model, embedding or judge calls. All counterexamples and claims are
limited to stated finite bounds; no absolute zero-bug claim is made.

Final targeted TLC counts (generated / distinct): supervisor 423 / 171,
correlation 104 / 62, physical knowledge 298 / 178; all positive exits 0.
Five seeded profiles exit 12 at the named invariant, including physical replay
at two actual invocations (381 / 230 states). These counts cover only the listed
bounded models, not production schedule coverage.

## Exact-argv integration wiring correction

The first frozen `d755136` unified run genuinely failed ordinary
`plugin-process-integration`: its preexisting audit rejected the two new worker
commands (`PROCESS_FIXTURE_REJECTS_OTHER_COMMAND`). Standalone targeted child
execution had not checked this composition. That failure is preserved externally
at `/tmp/evidence-host-frozen-20261102T202215Z-ordinary/plugin-process-integration.log`
and bound by `evidence/fixture-wiring-SHA256SUMS`; it is not called environmental.
The same full run also had an actual overall host timeout at the SIGINT test and
18 SOCKS ImportErrors in 1,071 completely discovered contracts. Formal-only
passed all 15 actual TLC runs. No tracked file changed during that frozen run.

After the run reached terminal and the scoped registration fix was approved,
the fixture now installs its two static workers through the existing
ProcessPackages manager. Their complete, exact launcher argv are declared in
`commands` before the existing audit is installed; execution stays after audit
installation. The audit function AST is unchanged (digest in the summary).
No general command, network exception or scanner allowlist was introduced.
Both workers additionally exercise an unregistered command and a tampered argv;
each must fail with exactly `PROCESS_FIXTURE_REJECTS_OTHER_COMMAND` before any
child executes. The full existing process-integration component then passes
under that audit: two real UNKNOWN cases and four rejected-launch controls.

`fixture-wiring-summary.json` records the focused results and raw-artifact digest
without embedding the large full protocol JSON. The follow-up frozen commit
still requires fresh ordinary and formal-only unified gates; focused success
does not replace those results. Seven owner/structural contracts passed after
this wiring change, including registration-before-audit / execution-after-audit.
