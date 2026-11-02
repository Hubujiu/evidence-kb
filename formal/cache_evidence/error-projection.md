# Generic model error observations

The actual direct `AgentModel` retained a known 15-token result when subsequent
diagnostic recording raised an uncoded `OSError`. `ModelEndpoint` rethrew that
error, and `ServiceEndpoint` correctly hid its diagnostic text but also discarded
the structured outcome. The public `ModelClient`, turn trace and persisted
`model_failed` event consequently lacked attempts and usage. The underlying
global and turn ledgers already retained their correct cost: this is an
observation defect, **not** a refund, budget bypass or instruction to retry.

## Mature boundary and minimal change

[OWASP's error-handling guidance](https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html)
separates a generic external response from internal diagnostic details. This
change retains the existing `CONTRACT_IMPLEMENTATION_FAILED`, HTTP 500, fixed
`Service implementation failed` message and `PlatformError` classification.
Neither exception text, arbitrary attributes nor cause text becomes public.

The established `model_errors.error_details` normalizer supplies its existing
allowlisted structured outcome fields; it is not replaced or modified. Generic
errors explicitly override the normalizer's inferred code with the fixed public
code. Coded errors retain their existing `kind=model` path and HTTP 502. Cleanup
still removes the owned receipt first, then bare-raises non-`Exception`
cancellations with their original identity. No send certainty is inferred merely
from dispatching an RPC, and absent facts remain unknown.

This is not a general secret sanitizer for arbitrary provider-defined nested
`call_record`, `trace` or `output_diagnostics` contents. Those are trusted existing
structured fact boundaries. The regression plants a synthetic private canary in
exception text, cause text and a non-allowlisted attribute, and checks actual JSON
errors and upper events do not expose it. It does not claim that arbitrary
hostile data inside a permitted outcome field is sanitized.

## Finite mapping

`ErrorProjection` explores one invocation, three error classes (generic, coded,
cancellation), and five observation classes: absent, proven unsent, unknown
attempt, observed zero and observed positive usage. Positive cost is abstracted
to one; this is not a proof of arbitrary token arithmetic.

`Begin` owns a receipt slot. `Fail` abstracts the already-completed AgentModel
ledger settlement followed by endpoint slot cleanup. `Project` constructs the
safe wire error; `Observe` copies its facts into the caller's failed event.
`Cancel` propagates the original non-Exception without constructing an error
response. These are distinct boundaries, not an atomic ledger/wire/event commit.

The positive model preserves observed facts, unknown classification, ledger cost,
safe generic diagnostics, released failure slots and cancellation identity.
Three controls independently drop generic facts (the legacy behavior), expose
private diagnostic text, or invent a known-unsent classification. They must fail
their respective named invariants. The AST guard is independently mandatory in
ordinary and formal lint; source-hash refresh alone cannot admit its mutants.

## Actual evidence and limits

The paired direct/public tests use a real `AgentModel`, `PluginHost`,
`ServiceEndpoint`, JSON error reconstruction and `ModelClient`. Additional tests
exercise real `knowledge_service.turn` trace and persisted `model_failed` events,
known zero, known unsent, unknown and early unobserved failures. Existing receipt
contracts retain the 64-cancellation identity/cost/slot checks and coded-error
behavior. All providers are synthetic; no paid calls or real credentials occur.

The baseline records three actual assertion failures. It also ran nine existing
fixture tests due to a direct test-class import; the final module imports the
fixture module instead, avoiding duplicate discovery. The original log is
preserved rather than relabeled as a product or tool failure.

Unbounded invocations, arbitrary usage values, malformed structured facts,
distributed response loss, storage/event failures, cleanup interruption and
Python-to-TLA refinement remain outside this bounded proof. This model does not
establish delivery liveness, nor replace ordinary full-project acceptance.
