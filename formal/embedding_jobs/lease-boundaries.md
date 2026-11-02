# Exact embedding lease and recovery boundaries

`EmbeddingLeaseBoundaries` supplements, rather than replaces, `EmbeddingJobs`.
The original cancellation/physical-send/rollback model remains binary-time.
This companion focuses on exact database-clock predicates, retry accounting,
circuit admission and the unlocked recovery snapshot followed by a conditional
settlement. This is additional model/contract coverage: **no production bug or
production behavior change is claimed**.

## Established patterns

The following primary sources were inspected before implementation on 2026-11-02:

- [Amazon Builders' Library: leader election](https://aws.amazon.com/builders-library/leader-election-in-distributed-systems/)
  describes the separation between lease ownership and delayed work, and between
  durable completion and notifying the caller. It warns that clock behavior is
  part of a lease's assumptions. This model does not replace database wall time
  with an application timer, and does not prove correctness under clock reversal.
- [etcd 3.6 concurrency API](https://etcd.io/docs/v3.6/dev-guide/api_concurrency_reference_v3/)
  combines ownership identities with transactions. Here the existing physical
  request ID and expected job status fence settlement; the code is not adopting
  etcd or claiming equivalent distributed-lock guarantees.
- [SQLite isolation](https://www.sqlite.org/isolation.html) describes serialized
  writers and `BEGIN IMMEDIATE`. The existing SQLite adapter takes its write
  transaction before reading the job. Serial order, not simultaneous eligibility
  of two predicates, determines the persisted outcome.

No upstream runtime, remote embedding, model, OCR or judge was executed.

## Exact comparisons and linearization

| Operation | Concrete guard | At `now = lease_until` |
| --- | --- | --- |
| `_claim` on RUNNING | `lease_until <= db_now` | job and matching attempt become UNKNOWN; no new request |
| ordinary `_finish` | request ID + RUNNING + `lease_until >= Clock()` | may settle if it wins the transaction order |
| `reconcile` expiry scan | RUNNING + `lease_until < Clock()` | does not expire the row |
| recovered `_finish` | request ID + UNKNOWN; no live-lease requirement | may settle only after a matching UNKNOWN snapshot/evidence |
| known-unsent `_finish` | request ID + RUNNING/UNKNOWN; no live-lease requirement | RETRY and refund once, never zero-cost merely because time expired |
| `_claim` retry/circuit admission | block only while deadline `> db_now` | equality admits, provided both gates and status permit |

At equality, finish-first yields COMPLETE and a subsequent claim reuses it.
Claim-first yields UNKNOWN and rejects the ordinary callback; exact recovery may
then complete it. Both are legal. Reconcile's strict comparison is modeled
separately and is not silently made equivalent to claim expiry. Each mutation is
one owner-local database transaction, not an assertion that external execution,
ledger evidence and persistence are one atomic system.

## Model mapping

| Model action/state | Source |
| --- | --- |
| `Claim`, `job.count`, `attempts` | `_claim`, JOB_CLAIM and ATTEMPT_INSERT; fresh request identity, attempt increment |
| `Expire(TRUE)` / `Expire(FALSE)` | claim's `<=` branch / reconcile's strict `<` scan, with ATTEMPT_EXPIRE |
| `Tick`, `job.until/retry`, `circuit.until` | ordered integer representatives of before/equal/after deadlines |
| `Observe`, `physical` | already validated observation category; not proof of capture authenticity or vector validity |
| `Settle(..., FALSE)` | `_finish` guarded JOB_FINISH, ATTEMPT_FINISH and circuit update in one transaction |
| `SnapshotRecovery`, `Settle(..., TRUE)` | UNKNOWN read, external recovery evidence, then request/status CAS; stale snapshots may remain after another worker settles/retries |
| `charged` / `AttemptAccounting` | ghost set of attempts not refunded; known-unsent/blocked preflight refund, observed HTTP errors do not |
| `circuit` / `circuitExpected` | ordinary provider failures increase failures and open deadline; success resets; preflight/known-unsent do neither; independent ghost accounting detects omitted failure/reset writes |

`ambiguous` is a local failure classification, not a durable physical outcome:
physical evidence can arrive later and recover the UNKNOWN job. A known-unsent
result is trusted evidence, not an inference from the job's clock/status. A retry
uses a new ID even after refunding the charged attempt. `http` means one of the
source's retryable HTTP statuses; other observed statuses are `fatal` and
nonrecoverable preflight is `preflight`. Recoverable preflight is `blocked`.

## Bounds, controls and limitations

One job, two fresh IDs, clock `0..5`, deadlines `0..6`, unit lease/retry/circuit
intervals, two charged attempts and circuit threshold one. Time advances only
between modeled transactions. The model has no fairness/eventual-success claim.
Source defaults remain unchanged. The model does not enumerate the full cross-product
of error attributes: known-unsent plus an HTTP retry status can retain backoff in
production; a dedicated SQLite contract covers that combination. `charged` is
only the persisted attempt counter, never a physical-spend or budget ledger. This bounded representative domain is not a
proof for arbitrary durations, counts, clock jumps or per-statement sampling.
The original model still covers cancellation/send/commit/ack separation; the two
models are not a mechanically proved composition.

The eight deliberate mutants each isolate a named invariant:

| Mutant | Expected failure |
| --- | --- |
| LateFinish | `LeaseSettlementFenced` |
| RecoveryRequest | `RecoveryCAS` |
| RecoveryState | `RecoveryCAS` |
| EarlyRetry | `ScheduleRespected` |
| RefundHTTP | `AttemptAccounting` |
| ReconcileEquality | `ReconcileStrictExpiry` |
| CircuitFailure / CircuitReset | `CircuitAccounting` |

Five additional configurations leave **all mutation switches false** and use
intentionally false exclusion assertions to require reachable equality finish,
equality claim expiry, recovered completion, recovered known-unsent and delayed
retry-at-equality. These are success witnesses, not seeded product defects or
liveness proofs. The existing runner's expected-invariant interface checks their
exact TLC exit and named violation, separately from the passing positive model.

`embedding_lease_contracts.py` executes the real repository, SQL operations and
SQLite transactions. A connection-local `strftime` function forwards to SQLite
with a fixed UTC instant; production SQL, comparisons and configured limits stay
unchanged. Tests move that explicit clock by one millisecond, never sleep to
force a boundary. Separate contracts cover cross-job circuit admission, stale
recovery callbacks, mismatched receipt fields and vectors, actual default limits,
persisted recoverable-ERROR compatibility, rollback on circuit write failure,
and an exception **after** commit acknowledgment is lost. The latter distinguishes
committed durable rows from caller knowledge, not simulated OS/power-loss testing.

Remaining obligations include PostgreSQL/MySQL concurrency, multi-batch circuit
interference in TLA+, actual provider evidence/transport integrity, clock sampling
inside transactions/reversal, arbitrary retry delays and limits, crash durability,
and machine-checked source refinement. Source fingerprints are review alarms,
not evidence that all source transitions or state machines have been proved.
