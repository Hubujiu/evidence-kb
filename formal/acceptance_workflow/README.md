# Acceptance preparation ownership (partial)

## Mature reference before design

- Reuse this repository's PR #36 reviewed `sqlite_guard` at local
  `08fb15fe49f58e7a7d8cc97679f0f53f42edf30a` (published
  `038e8b2b6f0036e4dc0221eaafe962a456d2c295`): SQLite `BEGIN IMMEDIATE` supplies
  process exclusion, `connection.close()` releases it, and the guard file is not
  unlinked. This feature is stacked on that receipt change, not a second lock
  implementation. A competing preparation fails immediately (`nowait=True`);
  there is no TTL, heartbeat or forced takeover during a long HTTP request.
- [SQLite's atomic commit explanation](https://www.sqlite.org/atomiccommit.html)
  distinguishes locking and durable publication from multiple independent writes.
  A local receipt and a remote HTTP effect cannot be one transaction.
- [Temporal's activity definition / idempotency guidance](https://docs.temporal.io/activity-definition#idempotency)
  explains that an effect may execute before its completion is recorded. Therefore
  releasing a dead process's lock is **not** permission to replay its operation.
  We preserve this project's stricter explicit-reconciliation contract rather than
  introducing Temporal-style retries for non-idempotent scope creation.

## Concrete counterexample and fix

Baseline `c8ef7582f226c07f1c06d3874dba159708ca7fad`:
`delivery_http.prepare_http` creates/reuses an output directory and reads its JSON
state. Nested `once()` consults that process-local snapshot, persists STARTED, sends
HTTP and records the result. No lock covered that sequence. Producer A can pause
immediately before persisting its first STARTED, after both the initial plan and
empty operations are on disk. Producer B reads that same state, creates a scope,
and dies before recording completion. A then overwrites B's STARTED from its stale
snapshot and creates a second scope. Atomic JSON replacement alone does not prevent
this. `Platform.create_kb` generates a new UUID for each real HTTP request.

The fix holds the existing SQLite guard across **all** preparation state loading,
operation claims, HTTP calls, receipt writes, authority binding and final artifacts.
`rebind_http` retains its exclusive destination mkdir and holds the same guard for
all destination binding writes. After acquiring the guard it rechecks that only
the guard file and its SQLite-owned `-journal`/`-wal`/`-shm` sidecars are present: a preparer that won the mkdir-to-lock gap cannot have
its receipts overwritten. It does not alter its completed source artifacts.
JSON receipts now use unique exclusive temporary files and flush/fsync before
replacement. A failed flush cannot replace existing STARTED with COMPLETE.

No pending gate is relaxed: STARTED, UNKNOWN and ERROR all block automatic reuse;
only COMPLETE reuses its recorded response. Process death releases the lock, but
its persisted receipt remains authoritative. There is no recovery/reconciliation
API added by this patch, no paid request retry, and no blanket HTTP idempotency claim.

## Model / mapping

`AcceptanceWorkflow.tla` models one output and one operation, two producer IDs,
and at most one restart per ID. Actions are acquire, read, claim, send, observe,
release, crash and restart. The local snapshot is a distinct step from the durable
receipt. Crash is possible at every active phase, including before claim and after
remote execution but before its receipt. COMPLETE/ERROR/UNKNOWN are different
observations; none grants a new send for the same operation.

| Model action | Concrete owner |
|---|---|
| Acquire / release | `prepare_http` / `rebind_http` and PR #36 `sqlite_guard` |
| Read / reject / reuse | `_prepare_http_locked` reload and nested `once` |
| Claim | `once` writes STARTED using `_atomic_json` before `client.request` |
| Send / observe | HTTP request, returned response or exception, receipt replacement |
| Crash / restart | OS releases SQLite lock; new invocation rereads persisted receipts |

Safety invariants: `TypeOK`, `NoDuplicateEffect`, `MarkerBeforeEffect`,
`ExclusiveOwner`, `CompleteHasEffect`. No fairness/liveness theorem is claimed.
The no-guard negative control reproduces concurrent stale snapshots. A separate
forget-pending negative control shows that a lock alone cannot prevent crash replay.
Both must violate the named `NoDuplicateEffect` invariant, not time out or merely
fail parsing. Results are generated with the repository's pinned TLC gate.

## Regression and mandatory lint

`acceptance_workflow_integration` is included in the existing mandatory foundation
integration stage. It runs the production FastAPI application over real loopback
HTTP, real SQLite storage and two OS producers. A test-owned barrier controls the
first claim; the second producer uses `os._exit` immediately after the actual scope
POST returns, before recording completion. The fixture rejects **every** other
mutating URL, so upload/build/Embedding/model/judge calls cannot occur. Assertions
check one actual scope, a busy contender, process death releasing the guard,
STARTED/UNKNOWN/ERROR preservation and zero additional scope creation on restart.
These are synthetic engineering fixtures, not enterprise RAG quality evidence.

Offline contracts verify the real guard is held on both entry paths, rebind's
exclusive output/source preservation, a real-file competing writer in the
mkdir-to-lock gap, and failed receipt flush. The formal registry
calls `acceptance_workflow_violations` as mandatory structural lint. Removing the
owner guard or post-acquire empty-destination check, allowing pending receipts or
moving/deleting the STARTED persistence
fails even after source hashes are refreshed; tests invoke the actual registry lint.
This is a focused AST guard/order check, not a general Python semantics proof.

## Evidence honesty / remaining obligations

- `evidence/baseline-runtime.log`: initial fixture entrypoint mistake (`__main__`),
  **not** a product counterexample. Preserved, not hidden.
- `evidence/baseline-runtime-fixed-fixture.log`: corrected fixture on baseline;
  one expected failure: **two actual scopes** for the same operation. The separate
  process-death/restart blocking check passes.
- `evidence/fixed-runtime.log`: initial c8-based fix, 2/2 passes; this is not evidence
  for the final PR36-stacked tree. Final-tree runs are recorded separately.
- First formal attempt rejected missing explicit discovery exclusions for the two
  integration test observers. They are now narrowly classified as tests. A later
  interrupted attempt with an empty log is not counted as a pass.

Assumptions: SQLite/filesystem locking works and successfully fsynced/replaced
receipts survive **process death**. Power-loss directory-entry durability, Windows
filesystem details, network filesystems, malicious deletion/changes and arbitrary
input size are not proved. The finite model is not source refinement. Binding
artifact/authority transitions and `DeliveryHTTPAdapter.run` remain unmodeled here.
Physical capture status and budget-ledger settlement are separate authorities;
this lock does not cover PR #34's cross-ledger settlement obligation. None of these
limitations is converted into an all-state-machines/no-bugs claim.


The first stacked full-check attempt (`.local/acceptance-stacked-full-01`) failed:
architecture rejected code comments/docstrings; the old host and offline contract
stages timed out at their unchanged 180-second limits. Comments/docstrings were
removed while that attempt was still running, so it is explicitly **mixed-tree
 diagnostic evidence**, not validation of any commit. Its summary and failure logs
are preserved under `evidence/mixed-tree-full-01-*`. No threshold was relaxed.
The earlier completed `06a8e4b` targeted/formal runs remain separately valid.
Final validation after the destination-gap correction uses fresh output directories
and a frozen commit; `.local` outputs are not rewritten or counted as a prior PASS.

The first empty-destination fence wrongly rejected SQLite's live rollback journal,
and its lint expected unparenthesized generator text. The real SQLite contract
caught both before final validation (`evidence/rebind-fence-initial-failure.log`).
The corrected fence permits only the exact guard database/SQLite sidecar names,
not receipt files or arbitrary paths; lint compares canonical parsed expressions.
