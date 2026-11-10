# Observed facts at the settlement exception boundary

This independent bounded model addresses the observation-publication bug found while
combining PR34 with PR31. `PhysicalBudget.tla` has observed and ledger usage, but did not
have the usage actually reported to the caller. Its old green result is **not** formal
coverage for that new bug. The six original physical-budget TLA/CFG files, properties,
positive runs and negative controls are unchanged.

## State and bounds

`ObservedSettlementFacts.tla` models one physical invocation. Reservation and settlement
each choose acknowledged commit, definite rollback, lost acknowledgement after commit,
or lost acknowledgement after rollback. Durable `budgetState` is separate from those
observations and from `sends`. Reserve-unknown-after-commit can therefore leave a durable
STARTED reservation while the caller correctly reports zero sends and no upper-level
model usage. No unknown reservation is refunded, no second send/finish or retry exists.

`observedUsage` and `reportedUsage` are **different state variables**. Four finite classes
represent raw provider observations:

| Class | Concrete abstraction | Upper accounting when one request was sent |
|---|---|---|
| missing | No valid provider usage dictionary; no invented usage field | One unknown-usage call |
| unknown | A dictionary such as `{}` with no complete token measurement | One unknown-usage call |
| partial | A known prompt count with missing completion/total, e.g. `{"prompt_tokens": 1}` | Known field retained; one unknown-usage call |
| known | Prompt 1, completion 1, total 2 | One call, two observed tokens |

These classes are not the ledger resource vector. `reportedSent` separately distinguishes
unsent, sent and unknown. A missing response stays unknown rather than becoming zero.
`errorOrigin` distinguishes reserve/pre-capture, transport, settlement and final capture
failures. A final capture error cannot replace an existing settlement/transport failure.
`Publish` represents exception/result facts becoming available to the public consumer;
`upperCalls`, `upperTokens` and `upperUnknown` expose their accounting consequences.

Final capture success/failure is nondeterministic. Persistent capture completeness and
UNCONFIRMED workflow admission are still the responsibility of the separate existing
`CaptureSettlement` model. This new model does not infer persistence from exception facts.
`Terminal` abstracts received COMPLETE/ERROR budget outcomes; a lost response instead
settles UNKNOWN. Unknown commit branches assume a decided durable commit/rollback, not
that arbitrary late/in-flight database operations are resolved.

## Source/action mapping and guard

| Model action/state | Reviewed implementation |
|---|---|
| Reserve / PreCapture; zero sends despite uncertain reservation | Unchanged PR31 pre-dispatch block in `MeteredTransport._handle_admitted` |
| Dispatch / Receive / LoseResponse | `inner.handle_request`, response read/decode and captured HTTP/body facts |
| Settle; durable result independent of reported send facts | Single `settle` helper; UNCONFIRMED before the only `ledger.finish` call |
| Capture and retained prior error | Final `save`, original exception note / bare raise |
| Publish; observed versus reported usage | Nested `preserve_observed`, canonical `model_errors.preserve_result_failure` and `ERROR_FIELDS` |
| Upper call/unknown/token accounting | PR31 `AgentModel.complete` and global/turn `BudgetLedger.settle`, exercised through serialized public SDK contracts |

The nested `MeteredTransport._handle_admitted.preserve_observed` is now independently
source-bound as well as covered by its outer method binding. The original canonical
module binding is retained. The mandatory settlement lint additionally requires the
actual response snapshot **before** the fallible settlement call. A contract moves that
snapshot after settlement, refreshes the source hash, and still requires semantic lint
failure. Existing mutants for loss of propagation, raw-usage substitution, zero-send
scope expansion, duplicate settlement and consumer-gate removal remain mandatory.

## Actual TLC results

Pinned TLC 1.7.4 / Java 21, one worker, 256 MiB heap, 60-second per-run bound:

| Run | Verdict / named expectation | Generated / distinct | Queue |
|---|---|---:|---:|
| `observed_settlement_facts` | Positive PASS; all ten configured invariants | 161 / 117 | 0 |
| `observed_settlement_drop_usage` | Exit 12, `ObservedUsagePreserved` violated | 82 / 78 | 39 |
| `observed_settlement_false_unsent` | Exit 12, `PostSendNeverReportedUnsent` violated | 82 / 78 | 39 |

The full unified `--formal-only` run passes **14** ordinary registered/control runs,
including all previously registered positives and negatives. The positive invariants
also cover acknowledged reservation before send, retained unknown-reservation authority,
known-zero-send accounting, reported response facts, no invented usage, conservative
upper accounting and preservation of prior failure. No liveness claim is made.

The two negative configurations are **seeded model variants**, not executions of the
historical Python source. Each changes one switch and isolates its named invariant.
Both actual eight-state counterexamples choose an acknowledged reservation, one send,
a known response, successful settlement, then a failed final capture. Dropping usage
reports `missing` and one unknown-usage call despite the known observation. Marking the
post-send error unsent instead reports zero upper calls despite `sends=1`.

The separate five real pre-fix Python assertion failures are preserved in
[`pr31-boundary-before.log.gz`](evidence/pr31-boundary-before.log.gz); those exercise
settlement failure and lost exception facts. They are not relabelled as these seeded
counterexample traces. Current concrete tests additionally cover capture-only failure,
partial usage, committed and rolled-back settlements, reservation uncertainty and
missing response.

Raw TLC output is losslessly gzip-encoded with zero timestamp:
[positive](evidence/observed_settlement_facts.tlc.log.gz),
[drop usage](evidence/observed_settlement_drop_usage.tlc.log.gz),
[false unsent](evidence/observed_settlement_false_unsent.tlc.log.gz).
Decompressed hashes are in [SHA256SUMS](evidence/observed-facts-raw-SHA256SUMS).
Raw tool frames were independently matched against parsed results; parser errors,
timeouts or unrelated invariants are not accepted as these negative outcomes.

## Remaining obligations

The model does not prove arbitrary token values/usage schemas, multiple attempts,
asynchronous cancellation, process death before publication, network/SQL durability,
cross-store UUID alignment, arbitrary exception mutation, or implementation refinement.
Its usage classes deliberately retain partial/unknown distinctions rather than modeling
every provider field. Source hashes and AST guards are review-drift/regression controls,
not a theorem translating Python execution into TLA+. All earlier family-wide remaining
obligations remain in the registry.
