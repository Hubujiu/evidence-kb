# Generic JSON transfer ownership

This is model-only coverage of the existing generic `JsonEndpoint` protocol,
with implementation-binding tests. No production transfer bug is claimed or
changed in this stage. The seeded premature-close behavior is a hypothetical
mutation used to verify the safety property is actually checked.

## Existing implementation reviewed first

`JsonEndpoint.execute` claims upload mode `dispatch` under its lock, then releases
the lock for hash/logical-request validation and handler execution. Close,
cancellation and expiration deliberately do not discard an in-flight dispatch.
The dispatch's `finally` owns final stream disposal. This resembles the mature
registry pattern of retaining a resource while a real caller owns it; cleanup is
not a substitute for proof that backend work stopped.

`JsonClient` attempts dispatch at most once. Its `request_dispatched` error marker
is set before the transport call and means a submission attempt, not confirmed
handler execution, database commit or model usage. Tests explicitly show that the
marker can be true even when an injected disconnect occurs before any endpoint is
reached. No operation-ID idempotency or automatic replay is added.

## Source-to-model binding

| Model | Python source |
| --- | --- |
| `mode`, `streamAlive`, `WriteChunk` | `_Transfer` and `JsonEndpoint.execute` ordered upload offsets |
| `Submit`, `SubmissionLost`, `submitted` | `JsonClient._call` dispatch attempt and transport failure before endpoint execution |
| `ClaimDispatch`, `VerifyGood/Bad` | `execute` dispatch ownership claim and length/hash/logical operation validation |
| `Invoke`, `Return`, `calls`, `actualEffects` | Single `_handler` invocation; driver errors do not prove zero effect |
| `Publish`, `largeResult`, `downloadAlive` | `_publish`/`result` result transfer, capacity/closed rejection and configured write-result error callback |
| `client` | Receipt/download observation, not underlying actual effect; a lost receipt stays unknown |
| `Close`, `DropIdleTransfer` | `close`, `_expire`, cancel and result-download cleanup; in-flight dispatch excluded |
| `ReadChunk` | `wire_read` and client response accumulation; final chunk releases download |

The Event-controlled implementation contract blocks the actual handler, rejects a
concurrent cancel, closes the endpoint and verifies the dispatch stream stays
open until the handler finishes exactly once. The final stream is then closed;
subsequent dispatch is rejected. Other existing tests cover checksum failure,
wrong offsets, interrupted upload/download and result publication failure.

## Bounds, assumptions and explicit omissions

One upload and optional result download, two chunks each, at most one handler
invocation and at most one actual mutation. A successful handler may be a no-op;
known completion does not prove changed bytes. Hash validity, publication capacity and receipt
delivery are finite booleans. The model separates submission intent, handler
execution, successful return and client knowledge. A handler exception can still
have an actual effect. No liveness is assumed for a blocked callback; stable
terminal states intentionally permit stuttering (`CHECK_DEADLOCK FALSE`).

This is **not** a model of `ObjectTransferEndpoint`'s concurrency: that owner holds
its lock across the object put, unlike generic JSON dispatch. Its existing actual
behavior contracts are included in focused checks, but formal coverage remains an
explicit obligation. Structured transaction lease/scope invalidation, multiple
simultaneous transfers, byte/count capacity, JSON/base64 details, cryptographic
hashes and integer/length overflow are also outside this bounded abstraction.
The model is not a source-refinement proof or a proof of external storage.

The negative config sets `DiscardInflight=TRUE`, causing close to release a stream
still owned by dispatch. It must fail `DispatchOwnsStream` with TLC exit 12. The
current implementation retains the stream and should pass. Logs and input hashes
are retained; the parent owns the full project gate and publication. Offline
synthetic faults only; new external model/embedding/judge calls: zero.

Initial local evidence (before no-op correction): transfer TLC 190 generated / 125 distinct states, negative
exit 12; whole formal runner PASS. The 33-test combined protocol run is recorded
under `../storage_value_call/evidence/python-focused.log`; seven additional actual
object-wire/application contracts pass in `evidence/python-object-wire.log`.

A review correction allows zero actual mutations even after successful handler
return. Original evidence is retained; `*-no-op-*` logs are the current model run.

Current corrected outcomes: TLC 226 generated / 146 distinct states; intentional
negative exit 12; formal runner PASS. The combined current 41-test result is
`../storage_value_call/evidence/python-no-op-focused.log`.
