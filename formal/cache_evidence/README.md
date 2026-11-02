# Cache, evidence and model-validation receipts

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
| `Fail` | Exception path removes reserved token |
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
endpoint close and `BaseException` cancellation are not checked. Provider
deadlines may be shorter in a concrete application, but host timeout and
`remaining_seconds` are optional and do not establish a universal bound here.
The model is not an induction proof for capacity 64, a refinement proof, a
cryptographic proof, or a guarantee about cache write outcome after an exception.

Actual regression, guard-mutation lint and TLC logs are retained in `evidence/`.
The jar was checked against SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`
and run on Java 21. The registry remains `partial`; StageCache and evidence
continuation obligations are not discharged by this receipt model.

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
