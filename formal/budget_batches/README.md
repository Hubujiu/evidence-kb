# Linked budget fencing and ledger-import intent

These bounded models describe existing implementation behavior. The actual
SQLite fault probes found recoverable partial sealing and a
fail-closed destination conflict, not a confirmed product defect. No production
implementation is changed by this stage.

## Mature boundaries, not transplanted algorithms

The pinned [DSH ce7073d token meter](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/token-meter/README.md)
is replay-based request-pressure/usage measurement; it is not this application's
persistent spend-budget authority. A measurement or cache miss cannot erase
previous physical UNKNOWN cost.

[SQLite transactions](https://www.sqlite.org/lang_transaction.html) provide
single-connection transaction boundaries, historic read snapshots and explicit
commit failure behavior. Separate parent guard files do not become one atomic
transaction merely because Python uses `ExitStack`. The logical rows hash in
`_snapshot` is retained; its physical file hash explicitly is not a WAL snapshot.
[Stripe's idempotency boundary](https://docs.stripe.com/api/idempotent_requests)
is a mature comparison for explicit operation identity and parameter binding,
not authorization to replay a physical provider request in this project.

## BatchSealing

Bounds: two parents, one chosen child plus one competing child identity, four
request IDs, independent child capacity two, one cost dimension and known-zero,
known-one or unobserved child usage. Each parent has one immutable historical
row. Initial parent IDs may collide; the first parent may already point to the
second parent, representing the permitted ancestor-to-parent-to-child chain.

| Model action/state | Source mapping |
| --- | --- |
| `Review`, `parentCost` | Attested old UNKNOWN rows remain unchanged; `_review_summary` changes admission status, not effective cost |
| `AcquireFirst`, `AcquireSecond` | Sorted `producer_guard(..., allow_sealed=True)` acquisition |
| `Validate` | Successor compatibility, frozen-parent integrity and duplicate-ID checks in `LinkedBudgetLedger.__init__` |
| `CommitSecond`, `CommitFirst` | Reverse `ExitStack` exit; each SQLite guard commits independently |
| `Abort`, `RetrySameChild` | Failed construction retains already committed seals; same-child reconstruction is permitted |
| `Reserve`, `Finish` | Parent revalidation/duplicate rejection and child `reserve(require_settled=True)` / terminal settlement |
| `ExternalDrift` | A changed predecessor snapshot blocks the next child reservation |

A returned child must have a persistent compatible predecessor chain, physical
IDs must not overlap its parents, reviewed UNKNOWN cost must remain, and only
one unresolved child row is allowed. Three independent negative controls remove
a seal, bypass duplicate rejection or incorrectly erase reviewed UNKNOWN cost.
They must violate their named safety invariant, not merely fail parsing.

The actual contract holds a real SQLite read lock on the first guard file. The
second guard commits, the first COMMIT fails with `SQLITE_BUSY`, and the observed
seals are `[unsealed, child]`. Rebuilding the same child after releasing the
reader succeeds. A sibling is refused; duplicate IDs are refused; parent files
are byte-identical. Three accumulated cost units with each independent cap two
are valid: these caps are not a merged spendable pool.

## ImportIntent

Bounds: one frozen source with two rows (one COMPLETE, one UNKNOWN), two possible
destination identities, empty/nonempty target classes and atomic target
row-plus-marker transactions. `PinIntent` is the explicit committed source seal
in `import_ledger`, before destination initialization. `Commit` is the target
transaction inserting the source rows and `ledger_imports` marker. `Fail` can
occur before or after that transaction. Same-dataset retry reads the marker and
returns without copying the rows twice; a different target is rejected.

The model checks persistent fencing before destination work, atomic rows and
marker, retention of the UNKNOWN row, no overwrite of preexisting target rows
and no migration fork. Negative controls omit the seal, omit the marker or drop
the UNKNOWN row. No all-or-nothing transaction across source and destination is
claimed.

The actual SQLite contract uses a trigger to fail the second row insertion,
proving rollback of both the first row and marker. Source bytes and the pinned
intent survive. Removing the synthetic fault and opening a **fresh bound handle
for the same dataset** recovers; another fresh-handle import is idempotent. The
old UNKNOWN retains 71 tokens, and the COMPLETE row retains 10. Reusing an
already sealed preparation handle is not the recovery protocol.

A nonempty destination remains pinned and refuses same-target retry; changing
the target is also refused. Both source and destination records remain intact.
This is a fail-closed conflict, not data loss and not automatic recovery. Operator
resolution of an incompatible/nonempty target remains an explicit obligation.

## Limits and evidence

Guards are local, all producers must use the fencing-aware protocol, and backend
transactions/locks and dataset identity are trusted. No fairness, availability,
operator-repair procedure, unbounded parent graph, cryptographic hash property,
WAL/filesystem correctness or distributed cross-database atomicity is proved.
Arbitrary resource vectors, actual usage exceeding a reservation, live backend
replacement and concurrent destination writers outside the guard remain outside
these abstractions. The physical-budget family itself is not newly modeled here.

Source bindings include the actual constructors, guards, review projections and
migration functions. Independently reviewed PR31 dependency anchors are retained
without copying unrelated receipt or pre-send product fixes. Historical run
summaries preserve exit codes and named negative controls; standard per-run
SHA256SUMS preserve the run's original source fingerprints separately from raw
TLC logs. They are not the live runner schema. All requests are synthetic and
owned; no external model, embedding or judge is invoked.
