# Structured transaction commit knowledge

## Defect and mature semantics reviewed before the fix

`_Handle._run` previously initialized its cached outcome to `ROLLED_BACK`.
If the transaction's `__exit__` committed SQLite and then raised during cleanup,
the first client call correctly became `CommitUnknown`, but a subsequent `finish`
returned a false rollback receipt. A failed rollback cleanup also received an
unproven rollback receipt. These are knowledge errors, not proof that SQLite lost
atomicity or that an external model request was sent.

The fix starts with `UNKNOWN` and promotes a terminal receipt only after the
corresponding context-manager exit returns normally. Original operation identity
is retained; provider `CommitUnknown` is not downgraded after cleanup. There is no
new storage layer, recovery retry, or generic operation-ID idempotency guarantee.

Primary references inspected before implementation:

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html): a failed
  commit can leave a transaction active; transaction error is not rollback proof.
- [SQLAlchemy 2.0 connections](https://docs.sqlalchemy.org/en/20/core/connections.html):
  connection/transaction context managers own commit, rollback and release. This
  project continues to use that existing driver instead of implementing SQL state.
- [FoundationDB unknown transaction results](https://apple.github.io/foundationdb/developer-guide.html#transactions-with-unknown-results):
  loss of commit knowledge can make blind replay execute a transaction twice.
  FoundationDB-specific retry/idempotency features are not inherited by this API.

The reference conclusions motivate the conservative receipt boundary; they do not
claim identical failure semantics across SQLite, FoundationDB and all drivers.

## Source-to-model binding

| Model variable/action | Implementation responsibility |
| --- | --- |
| `actual` | Real database effect, distinct from observer knowledge; existing `database_providers.base.transaction` owns commit/rollback |
| `RequestExit`, `exitCalls` | `_Handle._run` terminal action or cleanup path, one manager exit invocation |
| `BackendEffect` | Effect may commit, roll back, or remain active before a failing exit; no SQL implementation is modeled |
| `NormalExit`, `acknowledged`, `cached` | Normal `manager.__exit__` followed by terminal outcome promotion in `_Handle._run` |
| `FailedExit` | Generic exit/cleanup failure or provider `CommitUnknown`; legacy control recreates the old default rollback fallback |
| `Timeout`, `observed` | `_Handle.call` timeout can lose client knowledge while backend execution continues |
| `Observe` | `_finished`/endpoint repeated finish and `PortableStructuredClient.transaction` receipt handling; false covers missing, malformed or mismatched evidence |
| `IdentityPreserved`, `NoReplay` | Rust `transaction::resolve_commit`, original operation ID and `replay_allowed=false`; repeated observation is not backend reinvocation |

Registry hashes bind the reviewed Python symbols and Rust file. They detect drift,
not semantic refinement. `storage_commit_knowledge_contracts.py` exercises actual
SQLite through the production structured endpoint and native Rust validation.
Its AST contract checks UNKNOWN initialization, original operation identity and
normal-exit dominance of terminal promotions; mutants restore the old default,
promote before exit, and fabricate rollback in an exception handler. This is a
narrow structural guardrail, not a general Python control-flow proof.

## Bounds and explicit omissions

- One structured transaction, one exit invocation, three abstract database states
  and at most two receipt observations. SQL contents are abstracted.
- Normal driver acknowledgement is trusted. The model separates DB facts, server
  acknowledgement and client observation; UNKNOWN never implies zero work.
- Rollback requests may leave the backend active when cleanup fails, but do not
  independently commit. Commit requests can resolve either way or remain active.
- Explicit rollback, action-failure rollback and expiration cleanup share the
  same exit-evidence boundary; their thread/queue scheduling is not modeled.
- Guard heartbeats, handle capacity, queue admission, begin failure, outcome-cache
  eviction, schema/scope checks, DB/OS internals and arbitrary drivers are omitted.
- No fairness or liveness is claimed: external work may never complete.
  `CHECK_DEADLOCK FALSE` permits stable terminal states, not a progress proof.

## Reproduction and evidence

The positive checks type safety, no unsupported cached receipt, observer evidence,
no automatic replay and identity preservation. The negative profile changes only
`LegacyRollbackFallback` and must fail `NoUnsupportedReceipt` with TLC exit 12.
It must not pass merely because parsing or runtime evaluation failed.

`evidence/python-before-native.log` records the actual old-implementation failures;
`python-after.log` records the four new contracts passing after the fix. The focused
storage-suite and formal-run records are separate; none claims the full project
gate or live remote database acceptance. Initial setup diagnostics, if left in the
working directory, are not positive evidence.

From the repository root, with the pinned JAR and a freshly built native library:

```sh
python -m unittest kb_platform.rag_testing.storage_commit_knowledge_contracts -v
python -m kb_platform.rag_testing.formal_state_machines \
  --output /tmp/new-storage-formal-run --jar "$TLA_TOOLS_JAR"
```

All work is offline, with temporary SQLite files and synthetic faults. New model,
embedding and judge calls: zero. These bounded checks do not establish zero bugs.

Final local outcomes: formal runner PASS, positive model 90 generated / 47 distinct
states (depth 6), seeded legacy counterexample exit 12, 36 focused Python storage
contracts PASS, 18 formal infrastructure contracts PASS, targeted Ruff PASS. The
retained structured evidence was produced from a worktree based on historical
local infrastructure revision `f306692` (main `8160c051`); this local revision label
is not a GitHub commit address. The parent owns the full-project gate.

The central registry retains the broader transaction inventory with partial status
and explicit remaining obligations. This integration keeps Python explanations in
this document rather than source comments/docstrings, per the project source rules.
The [historical raw Rust test log](https://github.com/Hubujiu/evidence-kb/blob/8d5e1ee19d92e64b29710f00ed4f888e681e088c/formal/storage_transaction/evidence/rust-storage.log)
remains byte-for-byte in the published evidence snapshot, including its trailing
blank line. Current validation records are produced independently rather than
rewriting that artifact; the historical log does not establish CI success for the
final PR head.

## PR #39: phase-controlled idle-expiry regression

[CI run 41502706899](https://github.com/Hubujiu/evidence-kb/actions/runs/41502706899)
finished **FAIL**: all four quality jobs and formal checks passed, but the
integration contract gate failed the existing idle-expiry test. Its exact expected
error was `STORAGE_TRANSACTION_ROLLED_BACK`; actual was `DATABASE_QUERY_TIMEOUT`.
The failed test lasted 0.267537463 seconds. Its 0.2-second query budget and worker
lease started before the insert, so it did not establish the successful-write
phase before waiting for idle expiry. This remains a recorded CI failure, not an
accepted alternative result or a run relabeled as flaky.

The local base `896d61f9ed17a70823748eee657e74ed169977ca` has tree
`883dfc8b65b594241a73d0274dd99418d36d0cab`, identical to that failed CI tree.
`evidence/idle-expiry/ci-failure.json` preserves the raw failure excerpt, log hash
and timing. A separate deterministic local diagnostic advances the driver clock
only after a real SQLite INSERT returns rowcount 1. It reproduces the exact
`DATABASE_QUERY_TIMEOUT`, then verifies a cached rollback and zero persisted rows.
It is not a rerun of the old CI, and does not infer that the original failure was
unrelated to the tested change.

The corrected test keeps `max_seconds=.2`, real SQLite and the exact rollback,
repeated-receipt and zero-row assertions. Imported query/worker clocks are fixed
for this fixture. A test queue waits for the actual insert command without a
competing wall timer; only after the original `StructuredTransaction.insert`
returns rowcount 1 may its next **timed** get raise ordinary `queue.Empty`.
The production worker handles that exception through its existing idle-expiry
and rollback path. No rollback receipt, SQL result or production error is faked.
The handle is captured before confirming the insert because expiry can now finish
before the caller regains its thread. The existing join bound remains unchanged.

This fixture isolates idle-expiry behavior; it does not measure wall-clock query
latency or replace production deadline enforcement. Production code and limits
are unchanged. `storage_idle_expiry_fixture_contracts` adds structural guardrails
for real insert confirmation, timed-queue gating, untimed pre-insert waiting,
clock isolation, the unchanged budget and exact terminal assertion. Six seeded
mutants are rejected, including restoring the initial wall-clock queue race.
These AST checks are narrow review guardrails, not a Python refinement proof.

Local focused checks and lint use a freshly built native library, temporary local
SQLite and no external provider calls. Full-project and new-head CI validation
remain parent-owned; the failed original CI run has not been rerun or overwritten.
