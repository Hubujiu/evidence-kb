# Session writer and postflight authorization models

These are finite, executable abstractions of the current implementation, not a
proof of all session or authority behavior. Both inventory entries remain
**partial**. No product defect or production-code change is claimed here.

## Reuse the existing transaction pattern

`SessionStore._owner` takes the session row lock before every mutation. The
existing structured database transaction owns commit/rollback. The model uses
that pattern rather than introducing an application mutex or a second session
state machine:

- [PostgreSQL row-level locking](https://www.postgresql.org/docs/current/explicit-locking.html):
  the lock excludes conflicting writers until transaction end.
- [SQLite isolation](https://www.sqlite.org/isolation.html) and
  [transaction modes](https://www.sqlite.org/lang_transaction.html): a database
  has one writer, and `BEGIN IMMEDIATE` acquires the write transaction before the
  read/modify/write sequence.
- [Amazon Builders' Library: idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/):
  retain caller request identity and intent, reject changed parameters for the
  same identity, and return an equivalent result when the original response
  was not observed. The repository is stricter for interrupted work: it does
  not automatically reexecute it.

These sources explain the mature mechanism being abstracted; they do not prove
that every configured database provider implements the repository contract.

## `SessionWriterLease`

### State and transition mapping

| Source | Model action / state | Current executable regression |
| --- | --- | --- |
| `session_store.py:SessionStore.begin` | `Begin`: absent -> running; expired predecessor -> interrupted; expected revision and fingerprint gate | `session_database_contracts.exercise_session_database` |
| `session_store.py:SessionStore._owner` | One `lockOwner` across finish validation and commit/rollback; other session mutations wait | `SessionDatabaseContracts.test_real_sqlite_two_clients_obey_cross_database_contract` |
| `session_store.py:SessionStore._lease` | `LiveLease`, `PrepareFinish`, `Expire` | `exercise_session_database` checks stale append, success and failure |
| `session_store.py:SessionStore.append` | `AppendEvent`, bounded admission witness rather than arbitrary event payloads | `exercise_session_database` |
| `session_store.py:SessionStore._finish` | `PrepareFinish`, atomic `Commit`, `RollbackBeforeCommit` | `SessionStoreContracts.test_failed_commit_rolls_back_assistant_history_state_and_request_result` |
| `session_store.py:SessionStore._finish` failure branch | Expired, still-running writer may finish an error; superseded token may not | `SessionStoreContracts.test_replaced_writer_cannot_commit_failure_into_new_owner_state`; `RuntimeLifecycleContracts.test_expired_writer_can_atomically_close_failure_but_not_overwrite_successor` |
| Completed branch of `SessionStore.begin` | `Replay`: complete + identical fingerprint -> original revision/result; stale expected revision is intentionally irrelevant | `SessionStoreContracts.test_generic_backend_reopens_knowledge_sessions_with_artifacts_and_replay` |

Every listed test lives under `kb_platform/rag_testing/`. A request identifier
represents its unique generated token; the model does not reuse a token for a
different request. The owner/tenant check has already succeeded for modeled
mutations. Real foreign-owner refusal is covered by
`SessionStoreContracts.test_principal_and_tenant_isolation_cover_history_artifacts_and_replay`,
not claimed as a separately model-checked identity protocol.

The model checks:

- At most one request is running, including an expired-but-not-yet-superseded
  request. An interrupted request cannot later complete.
- A successful finish was admitted with a live running token. A failed finish
  needs a running token but may have an expired lease.
- Revision equals the number of completed turns; each cached response retains
  its completion revision, and a writer commits the revision it acquired plus
  one. Successful and failed turns both increment revision.
- Completion event, optional assistant event, state revision and cached response
  are one visible outcome. Ordinary appended events remain independent.
- Identical-request replay preserves fingerprint identity and never reexecutes
  interrupted work. Mismatched fingerprint / stale expected revision / busy
  writer / interrupted-request rejections are stuttering steps, not new work.
- A known successful observation requires durable completion. A lost reply
  after commit leaves durable completion plus an **unknown** caller observation;
  same-fingerprint replay can resolve it. A crash before commit leaves the
  request running plus an unknown observation, with no durable partial finish.

### Atomicity and lease assumptions

`PrepareFinish` samples lease validity while holding the row lock. `Expire` can
occur after that sample and before `Commit`: the admitted transaction may still
finish, because no successor can acquire the session lock in between. The model
does **not** assert that wall-clock lease validity is rechecked at physical
commit. Source code does not promise that stronger property.

The adapter's transaction is assumed atomic and isolated: staged effects are
not visible, and a precommit rollback undoes all effects. Commit and response
receipt are separate transitions, so a crash is not universally interpreted as
rollback. Storage-level commit uncertainty, power-loss recovery, isolation
implementation, UUID collision, wall-clock rollback, arbitrary event/artifact
payloads and multiple-session scheduling remain outside this abstraction.
Finite lease validity abstracts expiration, not elapsed real-time guarantees.

No fairness or liveness assertion is made. A stalled writer may require expiry
and a new request, and interrupted requests are not automatically resumed. The
configuration disables deadlock checking because quiescent/terminal states are
valid; it does not turn a liveness failure into a safety pass.

## `PostflightAuthority`

This separate model captures `KnowledgeTurn.run`'s observable ordering around
native completion, authorization, evidence revalidation, cancellation checks
and `SessionStore.finish`. It does not model the authority publication/build
state machine or claim that the two databases share a transaction.

| Source | Model mapping | Current executable regression |
| --- | --- | --- |
| `knowledge_turn.py:KnowledgeTurn.run`, initial authorize and cached branch | `Preflight`, `Replay` | `AgentGraphContracts.test_locked_sources_and_cached_answer_are_reauthorized` |
| Same function, after `runtime.run` | `NativeComplete`, three `CheckExecution` positions, `Authorize`, `Revalidate`, `BuildState`, `Commit` | `KnowledgeToolContracts.test_cancelled_delivery_publishes_neither_assistant_history_nor_last_answer` |
| `agent_tools.py:EvidenceGateway.authorize`, `read`, `revalidate`, `_unchanged` | A successful validation is represented at its own check point | `AgentGraphContracts.test_permission_change_before_verification_or_delivery_redacts_response` |

The fresh-result invariant says the success path performed post-native
authorization against the initial authority revision, current evidence
revalidation, and cancellation checks before attempting session commit. Cached
results must pass current evidence revalidation after authorization. The model
does not add cancellation behavior to the cached path, which does not perform
those fresh-turn cancellation checks in the source.

Successful gateway validation is abstracted as one action. The gateway's
internal multiple reads, immutable citation checks, revision discipline and
shared authority dataset protocol remain separate proof obligations. Native
execution, provider calls, budget behavior, database errors and redacted failure
payloads are abstracted away. `SessionWriterLease` covers the session commit
guard independently; these two modules are not a proved refinement/composition.

### Explicit limitation: validation is not a cross-store commit lock

The unmodified model has this counterexample to `CurrentAuthorityAtCommit`:

1. Native result completes under authority revision 0.
2. Post-native authority and evidence checks succeed at revision 0.
3. The last cancellation check succeeds; the turn is ready to commit.
4. Authority moves to revision 1 and scope access is revoked.
5. The separate session transaction commits; its stored witness has revision 1
   and `commitAllowed = FALSE`.

The witness records authority **at commit**, so this is not a false alarm caused
by revocation after an already completed operation. It establishes the absence
of the stronger cross-store guarantee. The documented implementation promises
checks followed by an atomic **session** commit, not a shared authority/session
transaction; therefore this is a **known limitation, not a confirmed bug or a
fixed defect**. Cancellation arriving after the final check has an analogous
unmodeled stronger requirement. Adding another ordinary read would only move
the window, not provide an atomic commit contract.

`PostflightAuthorityCommitLimit.cfg` intentionally expects this invariant to
fail. It is registered in `known_limitations`, separately from the three mutation
controls. The runner reports `kind: limitation` and `OBSERVED_LIMITATION`, never
`PASS`; it does not count this witness as a positive model or seeded defect.
The associated positive model uses the same module and identical non-assertion
configuration. An unexpected success, unrelated failure, timeout or missing
observation fails the gate and requires review of the declared limitation.

## Reproducible checks

Use the repository formal gate; `formal/registry-v1.json` is the single inventory
and run registry. For direct inspection, run each config from this directory with the verified TLC
1.7.4 JAR (SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`):

```sh
java -Xmx256m -cp "$TLA2TOOLS_JAR" tlc2.TLC -tool -seed 1 -fp 0 -workers 1 \
  -metadir "$NEW_OUTPUT_DIR/states" -config SessionWriterLease.cfg SessionWriterLease.tla
```

Each run needs a fresh metadir. Neither model requires a provider, a network
request, or a paid evaluation. The positive lease configuration has two request
identities and two fingerprints; expiration, rollback, response loss and replay
can interleave. The authority configuration has fresh/cached paths, one
authority mutation, scope/evidence permission booleans and cancellation.

| Config | Expected result |
| --- | --- |
| `SessionWriterLease.cfg` | All ten safety invariants pass |
| `PostflightAuthority.cfg` | All three safety invariants pass |
| `SessionWriterLeaseStaleWriter.cfg` | `NoSupersededCompletion` fails after expiry, successor begin and stale failure commit |
| `SessionWriterLeasePartialCommit.cfg` | `AtomicCompletion` fails on early assistant visibility |
| `PostflightAuthorityUncheckedReplay.cfg` | `ReplayResponseWasRevalidated` fails when replay bypasses the check |
| `PostflightAuthorityCommitLimit.cfg` | Known limitation: `CurrentAuthorityAtCommit` fails without changing the source abstraction |

Expected failures require TLC exit 12 and the exact invariant violation, not a
parser error, timeout, generic nonzero exit, or skipped execution. The positive
results are exhaustive only for these finite configurations, not proofs for
unbounded identifiers, all database providers or all authority workflows.

The initial source regression run executed all 13 tests from
`session_store_contracts` and `session_database_contracts` against the unchanged
implementation and passed. This includes real local SQLite contention, not
PostgreSQL/MySQL integration. The mapped native-runtime knowledge tests require
the repository's freshly built Rust artifacts and are not represented by that
13-test result.

Final direct TLC verification explored 44,129 distinct session states (152,181
generated, depth 13) and 216 authority states (349 generated, depth 12). All three
mutation controls and the separately labeled limitation witness produced their
expected invariant violation with exit 12. `verification.json` records these
bounded results; runner logs remain execution evidence, not a timeless pass.
