# Platform and auxiliary OCR transfer ownership

These are two independent repository protocol owners, not replacements for the
JSON transport, object-transfer endpoint, or page-local OCR recovery models.
The change adds finite models, deterministic contracts and mandatory review
guards; it does not diagnose or patch a production defect. Seeded negative
controls are deliberately wrong variants, not historical implementation bugs.

## Mature references and adaptation

- [S3 multipart upload](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html)
  separates a server-issued upload identity, ordered parts, integrity and terminal
  completion/abort. Here upload tokens are one-shot and byte offsets are strict;
  S3's replaceable parts, parallel upload and retry semantics are not imported.
- [tus core protocol](https://tus.io/protocols/resumable-upload#core-protocol)
  requires mismatched offsets to fail without changing the upload. The existing
  endpoint has that behavior; it is checked with actual bytes, not a new protocol.
- [gRPC cancellation](https://grpc.io/docs/guides/cancellation/)
  distinguishes an RPC cancellation signal from stopping application computation.
  Removing a pending token here does not undo an already consumed ingest/OCR call.
- [Python executor shutdown](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Executor.shutdown)
  distinguishes pending cancellation from work already running. The repository's
  actual `PluginHost` lease/drain contract, not this analogy, determines shutdown.

No reference is used as evidence that this implementation is correct. The existing
in-process locks, opaque UUID tokens and spooled files are retained; a new generic
transfer framework would duplicate owners with different lifetime contracts.

## Finite transition mapping

| Model | Actual boundary | Checked obligations |
| --- | --- | --- |
| `PlatformUpload` | `PlatformEndpoint.ingest_begin/append/finish/cancel` | Ordered complete input and hash before claim; result capacity reserved before consuming the upload; dispatch outside the upload lock; failed publication does not restore committed work. Inline publication releases its slot; transferred publication retains it until release. |
| `PlatformSnapshots` | `PlatformSnapshots.reserve/publish/read/cancel/close` plus original/ingest authorization callbacks | Reserved capacity counts before a stream exists; local spool ownership transfers on publication; failed publication releases the local stream; actor/kind binding, ordered offsets, sampled authorization, and live-snapshot recheck before stream access; terminal release closes the published stream. |
| `OCRUpload` | `AuxiliaryModelEndpoint.ocr_begin/append/ocr/cancel/close` | Complete image/hash before consumption; provider starts after unlocked claim; a consumed token cannot invoke it again or reopen after error; only validated OCR results return. |

Each model has two unique nonreused handles, two chunks and a symbolic one-slot
capacity, so both admission refusal and capacity reuse are reachable. This is not
induction over the real 8-upload/16-transfer limits or arbitrary byte budgets.
The snapshot model has one authorization change. `Age` means the relevant expiry
predicate becomes true: uploads expire strictly after their deadline; published
snapshots expire at the deadline. Reservations without streams do not expire.

Upload `Finish/Consume` and unlocked `Dispatch/ProviderStart` are distinct actions.
Host close waits for both claimed and running invocations. The raw endpoint has
no independent closed flag, by design: normal calls use host leases and cleanup
runs after drain. A real `PluginHost` contract verifies busy drain, rejection of
new calls, completion of the existing invocation and exactly-once cleanup.
Direct concurrent endpoint cleanup is not promoted to an independent shutdown
contract or used to demand a new production closed flag.

`PlatformSnapshots` additionally supports its own close/cancel during lock-free
authorization and stream construction. A close can leave a temporary spool owned
by the active publisher until that publisher observes rejection and closes it;
there is no fairness claim that a suspended publisher eventually resumes.

Authorization is observed at the callback, not when bytes arrive at the client.
A later revocation does not retroactively invalidate a chunk admitted by that
callback. Original content/identity changes are rejected by the real callback
contracts; the model collapses rejection reasons into one authorization change.
Nonreused tokens make a live token identify one object in the finite model. The
exact Python `is snapshot` recheck is separately guarded by AST lint; this is not
a model of UUID collision or same-token replacement.

## Verification and scope

All production ownership code remains unchanged. New actual contracts use real
temporary files, actual endpoints, `ServiceEndpoint`, and synthetic local
providers. They include overlapping finish/cancel, same-offset readers, close or
exact expiry while authorization is unlocked, result-capacity refusal, and an
ingest effect followed by JSON publication failure. These are component protocol
checks, not OCR quality or production RAG scores, and use no external model calls.
Existing public Platform/OCR contracts retain content hashes, actor/kind binding,
real repository authority changes and large-result round trips.

`rpc_transfer_owner_checks.py` is wired into mandatory registry lint. It checks
actual claim ordering, lock boundaries, snapshot recheck, result validation and
cleanup. Contract mutations refresh all source fingerprints before requiring
semantic rejection; drift hashes alone are not sufficient. Terminal witnesses
keep every safety switch enabled and must fail their named *No…Witness*
invariant. Their expected failure means a successful/failed/drained terminal path
is reachable, not that a product invariant failed. Missing, initial-only or
fault-enabled witnesses are rejected by the same mandatory gate.

Remaining boundaries include external provider behavior, real-clock correctness,
cryptographic token/hash properties, arbitrary input lengths, cleanup failures,
process crashes, durable ingest atomicity, host formal composition and full
Python-to-TLA refinement. Deadlock checking is disabled for stable terminal states;
no fairness, eventual completion, instantaneous revocation, automatic retry or
cancellation rollback is claimed.
