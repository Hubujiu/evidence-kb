# Physical owner accounting and LocalRanker settlement

This companion is **partial**, not a replacement for `PhysicalBudget`,
`CaptureSettlement` or `ObservedSettlementFacts`. The first three models and all
of their configurations remain byte-identical to stage `adf3d69`. The core ledger
implementation is unchanged; its new negative controls are explicitly seeded
model faults, not reproductions of a historical accounting defect.

## Why this adds a distinct obligation

The original client-knowledge model uses one resource, reservation2, actual1 and
cap4. Its `AdmissionBounded` property is valid for that instance; it is not the
production invariant `spend never exceeds cap`. A provider can report actual
cost beyond the estimate. The physical owner must retain that value and refuse
subsequent admission, even if the next requested vector is all zero. PR48's
application/turn ledger is a different authority; its model is not a substitute
for this physical owner obligation.

Missing usage is per coordinate: `actual.get(resource, reserve[resource])`.
Known zero must remain zero; an absent coordinate retains its own reservation.
This independent formula lifts dimension by dimension, but neither the bounded
model nor source fingerprints are a proof for arbitrary vectors. We model two
dimensions in one owner instance, not one repeated model per budget resource.
Status and usage are orthogonal: UNKNOWN may include a known partial vector,
while COMPLETE/ERROR with missing usage still retains unreported reservations.
There is no physical-ledger reconcile API. Receipt reconciliation and reviewed
predecessor admission do not rewrite its terminal UNKNOWN rows.

## State space and source correspondence

`PhysicalLedgerCore` has two physical IDs, two resources, cap2/reservation1 per
resource, and coordinate values missing (`-1`), known0, known1 or known3. Missing
is a model sentinel, not a valid negative production usage. Two finish invocation
slots represent competing callers or sequential repeated calls. Their atomic
linearizations explore either winner; request and finish identity duplication
are not equated with physical replay.

| Model | Source |
| --- | --- |
| `Reserve(r, strict)` | `BudgetLedger.reserve/_reserve`; duplicate, optional pending and per-coordinate capacity checks inside one write transaction |
| `Finish(c,r,state,observed)` | `BudgetLedger.finish`; STARTED-only UPDATE plus rowcount1 requirement; absent or terminal rows reject without changing facts |
| `Charge`, `ExpectedTotal` | `_sum_rows`, shared by `_totals`, `summary` and predecessor snapshots |
| `priorTotal` / `reservationAccepted` | Ghost observation of admission, not a persisted column or a live second read |
| `firstStatus`, `firstUsage`, `winners` | Ghost first-commit history for immutable-terminal/one-winner assertions |
| `Unresolved` / `strict` | STARTED or UNKNOWN blocks only when `require_settled=True`; default false still enforces capacity and unique ID |

`DimensionFallbackExact`, `TerminalImmutable`, `AdmissionBudgetSafe`,
`SettledPolicyRespected`, `AdmissionDecisionExact` and `NoUsageClipping` are checked
with `TypeOK`. There is deliberately no global `Total<=Cap` property.
Four independent controls respectively zero missing coordinates, omit the cap
check, permit terminal overwrite, or ignore strict pending admission. Each must
hit its own named invariant with a genuine TLC counterexample; parser failures
are never negative evidence.

The default-false policy is intentional. Google/OpenAI/local-rerank/linked
consumers request strict admission; MeteredTransport holds the producer guard
across pending read and reservation. Semantic review uses the generic default.
An unconditional single-unresolved invariant would misdescribe that API.

## Actual contracts and assumptions

`physical_ledger_core_contracts.py` runs real local SQLite transactions: mixed
partial/zero usage after reopen; retained actual3 against cap2 and subsequent
zero-vector denial; strict/default policy under STARTED and UNKNOWN; two separate
handles synchronized at competing finish, followed by all three terminal rewrite
attempts and a missing-ID rejection. Existing framework, ledger-race and shared
persistence contracts cover caps immutability, two-handle visibility, injected
backend binding and producer exclusion. Synthetic fault injection is not a
claim of actual network loss or power failure.

Atomic DB transactions, STARTED compare-and-set, exact resource keys, validated
nonnegative integers, caller-frozen caps and backend identity/locking are trusted
implementation boundaries. SQLite BEGIN IMMEDIATE and structured write locking
support the real contracts, but this model does not prove PostgreSQL/MySQL,
remote lease expiry, filesystem/WAL durability or late in-flight commits. Lost
commit knowledge is handled by the existing client model, not duplicated here.
No liveness, live backend replacement or unbounded induction is claimed.

Import requires an offline, quiescent source as documented in
[the storage migration procedure](../../docs/storage-plugins-execution-plan.md#显式搬迁数据).
Quiescence is an external precondition, not a consequence of the finite ledger
model. PR48 retains independent source/destination marker and linked-review
obligations.

## LocalRanker settlement boundary repair

A successful `finish(COMPLETE)` inside the inference exception handler permits
its own settlement failure to enter the error branch and invoke `finish(ERROR)`.
That second invocation can replace the first settlement exception with a terminal
CAS error. The regression contracts exercise rollback, lost acknowledgement and
inference-error context using the existing synthetic ranker fixture and local
SQLite fault injector; no model weights or external inference are needed.

The only production change moves success finish into the `try/else` suite.
Failure of that finish cannot enter the inference exception handler. The error
branch retains exactly one finish and bare rethrow. Settlement exceptions keep
their object/operation identity; if settlement fails while handling inference
failure, Python retains the inference exception as its context. Successful
score capture, actual resource quantities, exception boundaries and replay
blocking remain unchanged. No retry, refund or new capture durability claim is
introduced. The existing `PhysicalBudgetLegacy` counterexample already describes
this class; no duplicate model was added for the new consumer. LocalRanker has
no provider receipt, so only the at-most-once settlement subtrace/projection
applies, not a claimed refinement of every receipt action in that model.

Other directly identified callers were reviewed: MeteredTransport and Google
use settlement-started guards; model_http only performs its failure finish when
no response exists, so success finish cannot re-enter it; semantic review places
success finish outside the provider exception handler; LinkedBudgetLedger only
forwards, and the foundation fixture initializer has no catch/retry. This is a
static direct-call-site review, not proof against arbitrary dynamic aliases.

## Mandatory structural guard

`physical_ledger_checks` is invoked by mandatory registry lint when these source
owners exist. Its reviewed AST shapes protect per-coordinate fallback, atomic
capacity/pending/identity checks, STARTED-only update plus rowcount rejection,
summary projection, explicit default-false policy, and LocalRanker's separate
success/error settlement sites. Refreshed-source-hash mutants still fail the
semantic guard. This is intentionally narrow structural lint, not Python
control-flow refinement; substantive owner refactors require explicit review.
Only newly bound symbols are added; no existing reviewed source hash is blindly
refreshed. All other machine records remain unchanged.

## Verification scope for this code-only publication

This change includes implementation, regression contracts, mandatory source
checks and model/configuration inputs. New local execution logs and their
manifests are not included. Pre-existing published evidence is unchanged.
The current head must run the normal mandatory CI independently; earlier-stage
results do not establish a pass for this tree. The ordinary gate and actual
formal gate remain separate required checks.
