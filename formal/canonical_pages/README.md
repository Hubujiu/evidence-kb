# Canonical page publication: public library protocol

This feature models `CanonicalPageIngestion.run`. It is a public repository
library API with no current CLI call site found in the repository audit. The
contracts deliberately call that API; they do not claim a newly integrated CLI
or production ingestion route. No production defect was established by these
contracts. The negative models below are seeded controls, not bug reports.

## Mature references consulted first

- [Temporal Activity Execution](https://docs.temporal.io/activity-execution),
  inspected 2026-11-03: activity execution, its observable result, and retry
  policy are different concerns. A missing completion acknowledgment does not
  establish that physical work never happened. This library has no automatic
  retry policy: every existing output is refused and earlier ledger records
  remain authoritative.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html), inspected
  2026-11-03: a database transaction's atomic boundary does not automatically
  extend to separate JSON files or external work. The model keeps physical
  charges, capsule files, aggregate publication, optional build and the final
  report separate. It assumes the underlying ledger's transaction contract,
  which is independently modeled by the physical-budget feature.
- The implementation also reuses the existing capsule writer/importer rather
  than introducing a new publication format or a parallel receipt service.

## Finite source map

Two ordered sources each contain two ordered batches, with fresh request IDs
abstracted by batch identity in the correct protocol. Per-batch `sendCounts`
records actual sends. An unsafe existing-output reentry returns to admission
and performs another send for the original planned batch; `NoReplay` rejects
its count of two instead of relying on a preset violation flag. Both the
optional-build and no-build profiles are checked.

| Model transition | Actual source boundary |
| --- | --- |
| Begin/Plan | fresh output check, mkdir, exclusive plan.json write |
| Admit | source/state/policy/catalog validation and per-batch authorize sample |
| Send/ObserveGood/ObserveBad/ObserveUnknown | documents_observed; physical settlement belongs to provider/ledger; unknown and errors retain expense |
| Validate | receipt identity, text/vector/manifest/context and durable ledger checks in `_receipt` |
| Postcheck | authorize after each actual encoding |
| WriteCapsule | writer produces source capsule after all its batches validate |
| Readback | import_capsule, exact vectors, zero new embedding calls; only then append completed source |
| NextBatch/Prepublish | ordered loop and final authority sample |
| PublishAggregate | capsules.json exclusive write |
| Build | optional build_service.build with original expected state |
| PublishResult | successful final result.json and return |
| Fail | ordinary Exception report with actual registered IDs and ledger totals |
| Interrupt | uncaught BaseException or stopped process; prior artifacts/costs remain |
| Reenter | only the deliberately unsafe reuse-output control can replay an existing directory |

`charged` represents retained observed cost or unknown reservation, not an
exact token count. The model does not subtract already settled expense when a
later stage fails. Exact request payloads, hashes, vector dimensions and capsule
readback are checked by deterministic contracts, not by pretending boolean
receipt validity proves cryptographic correctness.

Seven unsafe variants target: lost unknown reservation, accepting an invalid
receipt, premature capsule write, skipped readback, premature build, omitted
admission authorization, and same-output replay. Five inverted invariants are
non-initial reachability witnesses for success, partial source failure,
unknown outcome, interruption after a send, and optional-build failure. Each
negative/witness must exit with its own named invariant; parse/type errors or
an Init-only trace do not establish the scenario.

## Concrete contracts and automated constraint

The unified contracts execute the real library, a real file-backed SQLite
BudgetLedger, actual capsule serialization and actual capsule importer. The
encoder/authority/build boundary is synthetic. Ledger `remote_embedding_calls`
units are synthetic accounting inputs; **no remote embedding/model/judge call
is made**. Tests cover two sources/two batches, ledger reopen, exact physical
identity, invalid vector digest, post-send revocation, known/unknown failures,
partial completion, writer/readback faults, optional-build success/failure,
interruption and report-publication failure. Existing output is never replayed.

The mandatory registry gate invokes `canonical_page_violations` whenever the
owner source is present. It checks direct rejection predicates and sequencing,
not just hashes. Contracts mutate nine distinct safety obligations and compute
fresh source hashes before checking that the semantic gate still rejects them.
The gate is a focused structural regression constraint, not a Python verifier.

## Explicit boundaries

- A successful finite run is not an unbounded proof or a machine-checked source
  refinement. Arbitrary provider code, embedding quality, external build
  internals, cryptographic collisions and distributed database behavior are
  outside this owner model.
- Authorization is sampled. Revocation may happen after the final check and
  before publication; the model explicitly permits that order and does not
  assert instantaneous revocation or manufacture a production race fix.
- Files are written directly with exclusive creation, not fsync/atomic rename.
  A crash can leave partial plan/receipt/capsule/result bytes. There is no
  automatic recovery of such files; reentry into an existing output is refused.
  The model treats these as absent/incomplete, never successful publication.
- A capsule may be written but absent from `completed_capsules` if readback or
  authorization fails. That report contains verified completions, not every
  file on disk. Physical charges remain available independently.
- Ordinary-error reporting can itself fail due to I/O or ledger unavailability.
  This model does not promise that every interruption has a readable report.
- No liveness, eventual success or remote cancellation guarantee is claimed.
  Raw local model/test artifacts remain private; CI supplies fresh run evidence.
