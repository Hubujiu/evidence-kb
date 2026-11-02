# Cache, evidence and model-validation receipts

Tracked historical summaries, raw logs, per-run SHA256SUMS and explicit nonce
redaction are described in [Historical evidence format](historical-evidence-format.md).

## Model receipt slots: confirmed defect and correction

`ModelEndpoint.complete` reserved `(deadline, None)` before invoking the model.
Another request could expire that **still-running** placeholder after 900 seconds.
The original completion then published its result unconditionally, exceeding the
64-slot limit. The public `ServiceEndpoint` regression uses a blocked synthetic
model and controlled clock: one old call plus 63 ready receipts must reject the
next request before invoking the model. The original code did not reject it.
An independent original-code run accepted 64 later requests and ended with 65
receipts. These are deterministic synthetic interleavings, not an observed
900-second production incident or a paid model run.

The fix only expires completed envelopes (`is not None`, not truthiness).
The old call retains its slot and its eventual result, attempt count and usage.
PR31's canonical error-detail serialization is unchanged. The mature reference
is [aiohttp v3.11.18 BaseConnector](https://github.com/aio-libs/aiohttp/blob/v3.11.18/aiohttp/connector.py):
`connect` counts an acquired placeholder while connection creation is pending,
then replaces or releases it when the operation completes. This is an ownership
pattern, not a claim that model requests are aiohttp connections.

| Model action/state | Actual implementation |
| --- | --- |
| `Begin`, `pending=running` | Locked expiry sweep, capacity test, `(deadline, None)` reservation |
| `Complete`, `pending=ready` | Actual model return, locked replacement with envelope and renewed TTL |
| `Fail`, `Cancel` | Exception and propagating BaseException paths remove reserved token |
| `Age` | Strict `deadline < monotonic()` predicate becomes true |
| `Settle` | Atomic token pop; freshness check; optional `accept_completion` |
| `accepted`, `confirmed`, `settled` | Ghost history distinguishing successful cache return, valid confirmation and consumption |
| `Close` | Clear pending receipts after host leases drain |

The finite model has three distinct nonces, two slots, one invocation per nonce,
and fresh/expired clock classes. It covers rejection, provider error, TTL sweep,
cache failure, successful confirmation and quiescent close. No fairness or
liveness property is claimed. Deadlock checking is disabled for stable terminal
states, not used as evidence of progress. The legacy negative control changes
only whether expiry may erase running placeholders and must violate
`CapacityBound` with exit 12. The positive invariants additionally check retained
ownership, confirmed consumption, no reappearance and empty closed state.

Assumptions: unguessable unpublished tokens; atomic in-process lock sections;
host-mediated close after all calls, including settle, drain. Direct concurrent
endpoint close and abrupt process termination are not checked. Provider
deadlines may be shorter in a concrete application, but host timeout and
`remaining_seconds` are optional and do not establish a universal bound here.
The model is not an induction proof for capacity 64, a refinement proof, a
cryptographic proof, or a guarantee about cache write outcome after an exception.

Actual regression, guard-mutation lint and TLC logs are retained in `evidence/`.
The jar was checked against SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`
and run on Java 21. The registry remains `partial`; StageCache and evidence
continuation obligations are not discharged by this receipt model alone.

## Capacity refusal outcome

A second actual regression follows `KnowledgeService.turn` through `ModelClient`
and `ServiceEndpoint(ModelEndpoint)`, using a real `AgentModel` with a synthetic
Luna transport. After filling the receipt slots, refusal makes no additional
provider call and leaves the real model ledger unchanged. However, the original
`model_failed` event omitted `attempts` and `model_sent`; `CallBudget.can_explore`
therefore counted one physical attempt. The correction attaches known-unsent
zero attempts at the actual pre-model capacity guard, not by inferring execution
from an error-code prefix or from RPC dispatch. It keeps the same error code and
HTTP status, and invents neither token usage nor physical request identity.

`CapacityRefusal` isolates this observer contract: initial occupancy 0–2, one
caller, one dispatch or refusal, then one observed failure event. `Admit` maps
to the guarded provider invocation; `Refuse` to the capacity exception;
`Observe` to the event and physical-attempt fallback in `CallBudget`.
`UnknownRefusal=TRUE` restores the missing fact and must violate
`RefusalReportsNoAttempt` with exit 12. This small model does not replace the
receipt concurrency model or the separate budget-accounting models. The actual
test verifies both trace and stored event, provider counter, unchanged ledger,
no new turn ledger, and the real `CallBudget.can_explore` decision.

## Exact cache admission and volatile payload loss

`StageCache.tla` models two **whole exact dependency keys**, two content hashes,
one read, and absent/correct/corrupt volatile payload classes. `PutIndex` maps to
the immutable key/hash transaction; `Remember` is the separate best-effort TTL
payload write. `Lose` abstracts expiry, eviction, clear or an unavailable payload
backend, all of which become a miss. `Corrupt` represents malformed or mismatched
payloads, which must not be delivered. `BeginRead` captures current authorization
and cold/live-under-test bypass; `Lookup` captures the exact index row;
`ReadPayload` checks the observed bytes against that row's hash. An immutable
index can outlive its payload, and repopulation is allowed only for its same hash.

The mature references are [Redis expiration](https://redis.io/docs/latest/commands/expire/)
and the repository's exact-dependency cache contract in `AGENTS.md`. TTL is never
an integrity or authorization substitute. The full key tuple is abstracted to
an atom; existing actual tests mutate every field and ordered upstream input.
Three seeded variants bypass admission authorization, select another key, or
skip content integrity; each must violate its corresponding named invariant.
These are test variants, not discovered defects in the current implementation.

Authorization linearizes at admission. A controlled object read that revokes
authorization after admission may still return its validated snapshot; the next
read must be denied. This is **not** response-delivery-time authorization.
Persistent filesystem/blob error handling, real key builders, collision
resistance and external storage isolation are not proved by the volatile model.
The existing `test_volatile_miss_does_not_release_unknown_budget_or_allow_blind_retry`
verifies real cache/budget composition; budget state is intentionally not copied
into this model or erased on a miss.

## Actor-bound one-shot evidence continuation

`EvidenceContinuation.tla` has three unique nonces, three actor/tenant tuples, two
pending slots and at most one claimed finish. `Prepare` abstracts publication
after the staged generator's first yield and its expired-pending cleanup.
`Take` is the locked actor comparison and deletion, the one-shot claim point.
`RejectOther` must leave ownership untouched. `Finish` collapses success,
cancellation, expired receipt, invalid ranking and resume failure into the same
`finally: pending.close()` resource-disposal effect. Exact deadline and outcome
behavior are covered by actual public contract tests, not individually proved
by that collapsed action. `Close` assumes host admission is closed and active
calls drained; its logical `closed` is an environment gate, not an endpoint field.

The mature analogy is [RFC 9700's transaction-bound nonce and code replay prevention](https://www.rfc-editor.org/rfc/rfc9700.html):
tokens are bound to the initiating context and consumed once. This endpoint is
not an OAuth implementation. Inspection confirmed the current implementation
already checks actor **before** deleting the token. The negative control instead
seeds the opposite behavior and must violate `ActorBoundConsumption`; no false
cross-actor product-bug claim is made. Tests cover wrong tenant/principal finish
and cancel, subsequent legitimate finish, every terminal cleanup, replay, exact
expiry boundary, stale-token purge, and existing downstream authority rejection.

Generator internals, parallel claims for different tokens, unpredictable token
space, direct endpoint calls after host close and full downstream retrieval
authority behavior remain outside this finite model. There is no liveness claim.

## Integrated dependency review and gates

`source-review-v1.json` preserves the independent PR31 review: eleven changed
anchors (the current `ModelEndpoint.complete` receives its own follow-up review),
three extracted transition bindings, five read-only projection bindings, and
twenty-one explicitly unchanged bindings. The canonical `model_errors` **whole
module**, including `ERROR_FIELDS`, is bound without changing it. No pre-send
transport implementation or separate normalizer is imported. Those other
families remain `unmodeled`; reviewing dependency drift is not model coverage.

Both ordinary source-quality lint and formal registry validation run the same
specific receipt-expiry, capacity-outcome and cancellation-cleanup AST checks.
A synthetic regression
refreshes a mutant's source digest and still requires formal validation to reject
the bad guard or unknown attempt facts, so hash refresh cannot bypass these rules.

The preceding candidate's integrated `uv run --no-sync python scripts/check_project.py --formal-only`
run passed all 19 actual model/control runs and both evidence-contract tests with
zero registry violations. Its compact evidence is
`evidence/integrated-formal-verification.json`; earlier stage summaries that list
dependency drift are preserved historical records, not the final verdict.
Ordinary whole-project acceptance and remote CI are separate requirements and are
not implied by this formal-only result.

## Propagating cancellation must release only its endpoint slot

The first receipt fix deliberately retained live placeholders but initially
excluded `BaseException` cleanup. A subsequent actual-host probe established that
the exclusion hid a reachable defect: `PluginHost.call` and `ServiceEndpoint`
propagate a provider's `BaseException` while the host remains ready for other
calls. The old endpoint's `except Exception` never removed those terminal
placeholders. Sixty-four controlled cancellations exhausted all slots, and
advancing the receipt clock did not recover them.

The correction follows the already cited aiohttp placeholder cleanup pattern:
catch `BaseException` to remove this token, then **bare raise** non-`Exception`
values even when they carry a `code` field. Normal coded exceptions still use
PR31's existing error normalizer. Cancellation is not converted into
`ContractError`, a zero-send claim, a refund, or a retry.

The real-host regression checks all 64 original exception object identities,
continued metadata access, empty endpoint slots, and a successful 65th synthetic
completion. The actual `AgentModel` ledger retains all 64 unknown-usage calls and
their conservative token charge, then adds the successful response's 15 tokens.
`Cancel` in `ModelReceiptSlots` removes the live owner and its placeholder;
`KeepCancelledSlot=TRUE` removes only the owner and must violate the new
`NoAbandonedReservation` invariant with exit 12. Abrupt process death, cleanup
failure itself and physical provider cancellation guarantees remain unproved.

`evidence/cancellation-contracts.log` records 27 passing focused contracts.
The cancellation-prefixed TLC records retain the revised positive model and both
negative controls separately from the earlier receipt evidence. The positive run
passes; the expired-inflight and abandoned-cancellation variants each fail their
expected invariant with exit 12.

The original ordinary full-gate run at candidate `4e849ef` is retained at
`/tmp/evidence-auth-cache-full-project-20261102-1913`: architecture, formal lint,
Ruff and scorer passed, but Rust commands could not find `cargo` on that process's
PATH and dependent stages were blocked. It was not a product or permission
failure. The final candidate is checked separately with the existing Rust 1.99.0
toolchain explicitly selected; this does not erase or reinterpret the old FAIL.

## Generic model error projection

[Error projection](error-projection.md) documents the later public-observation
fix, its bounded model, privacy boundary, actual-host/event regressions and
remaining obligations. Evidence is under `evidence/error-projection/`; the
original receipt and cleanup histories remain unchanged.
