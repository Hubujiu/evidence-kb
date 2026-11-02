# Object transfer endpoint and client ownership

**Model-only audit: no production defect is claimed or changed.** This covers
`storage_object_wire.py`, not the generic JSON dispatch model in PR41. The latter
releases its lock around dispatch; this endpoint holds an `RLock` through object
store calls. Copying its in-flight close behavior here would model the wrong code.

## Mature behavior consulted before modeling

- Python 3.13 [RLock documentation](https://docs.python.org/3.13/library/threading.html#rlock-objects):
  the owning thread may reacquire, whereas another thread waits until the final
  release. Therefore the model serializes concurrent close/cancel/expiry with
  put/read; it does **not** claim protection from arbitrary same-thread reentry.
- Python 3.13 [IOBase.close](https://docs.python.org/3.13/library/io.html#io.IOBase.close)
  and [SpooledTemporaryFile](https://docs.python.org/3.13/library/tempfile.html#tempfile.SpooledTemporaryFile):
  stream lifetime is distinct from a storage object's lifetime. Close attempts
  can fail; no claim says every failed close releases its OS resources.
- Amazon S3's [multipart lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html)
  distinguishes initiation, parts, completion and abort. We reuse the distinction
  between an incomplete transfer and a completed object, **not** S3's out-of-order
  parts/retry behavior: this project's offsets are ordered, checksum validation
  precedes `put`, and the client never automatically replays finish.

Reviewed current implementations: `ObjectTransferEndpoint`, `ObjectTransferClient`,
`ScopedObjectStore`, `FileBlobProvider`, and `S3BlobProvider`. Built-in provider
code has no callback to the endpoint. Future custom/reentrant providers require a
separate review; an RLock is not a non-reentrancy guard. The files provider's real
put writes, flushes, fsyncs and links content-addressed data. A late error or lost
response can coexist with a physically readable object. This audit does not call
an external provider or a paid model.

## Model-to-source mapping

| TLA+ state/action | Implementation boundary |
| --- | --- |
| `Live`, `mode`, `offset`, `Capacity` | `_handles`, `_Transfer`, `_capacity`, stream cursor |
| `BeginUpload`, `BeginDownload` | validated `_begin`; download verifies before allocating |
| `WriteChunk`, `ReadChunk`, `RejectedOperation` | exact ordered offsets, final download discard; invalid mode/offset still refreshes touch |
| `age`, `Tick`, `Expire` | monotonic touched time; lazy `>= HANDLE_SECONDS` sweep on a valid-shaped execute |
| `FinishBegin`, `ValidationFailure` | finish length/hash verification under the endpoint lock |
| `Put(effect, acknowledged)` | one actual store put; an exception/invalid ref may follow a real effect |
| `FinishCleanup` | finish's `finally: _discard(handle)`, including validation/put failures |
| `Observe`, `ClientCancel` | client response loss/error; final best-effort cancel never replays finish |
| `CloseBegin`, `CloseOne`, `CloseEnd` | closed seals admission, pop precedes stream close, failure may leave other handles for explicit close retry |

A preexisting object supplies download data. The successful witness establishes
both a successful upload and a complete download, not identity equality between
that preexisting object and the upload. The real round-trip contract checks actual
returned locator, bytes and digest equality for empty and multi-chunk data.

`closeAttempted` means cleanup was attempted, not that arbitrary OS cleanup
succeeded. The model intentionally does **not** assert `closed => no handles`:
a throwing stream close interrupts the loop after its handle was popped. A later
explicit close can clean remaining handles while execute remains rejected. It
also does not assert `closed => no storage effect` or `error => no effect`.

## Checked properties, finite bounds and omissions

Nine positive invariants cover types, declared capacity, ordered offsets, held
finish ownership, terminal handle retirement, validation-before-put, at most one
client put attempt, sound positive acknowledgment, and final download retirement.
Two positive profiles use 0-byte/2-byte uploads, a 2-byte download, 1-byte chunks,
two unique never-reused handle IDs, a three-byte aggregate budget, three idle-time
buckets and at most one close failure. Capacity is intentionally small enough to
reject simultaneous two-byte handles and permit a zero-byte upload plus download.
Production limits, lazy exact expiry, malformed replies and actual temporary-file
streams are exercised by the mandatory discovered implementation contracts.

Each API operation's validation/sweep is abstracted without interleaving another
request into a stale-handle sweep; all request actions require the sweep complete.
Malformed requests, rejected admission, allocation/download verification failures,
wrong-mode/offset operations with no cursor change, and cancellation of an absent
handle are stuttering or reduced transitions. The model splits finish and close
into steps but retains lock ownership, permitting elapsed time without allowing a
concurrent endpoint operation. No fairness or eventual completion is assumed for
a blocked provider, and intended terminal states allow stuttering.

Not covered: unbounded transfer populations, UUID collisions/reuse, arbitrary
reentrant/custom providers, cryptographic collision proof, raw OS stream failure
recovery, process restart, network exactly-once delivery, all serialization input
shapes, provider/OS durability, and machine-checked source refinement. Source
hashes are review-drift lint, not an equivalence proof. No all-state-machines or
unbounded bug-free claim follows from these checks.

## Negative controls and non-vacuity

- `LeakFinishedHandle.cfg` alone enables the **seeded unsafe** omission of finish
  cleanup; it must violate `TerminalHandleReleased` with TLC exit 12.
- `ReplayAmbiguous.cfg` alone enables a **seeded unsafe** second put after an
  ambiguous effect; it must violate `ClientSingleAttempt` with exit 12.
- `SuccessfulTransferWitness.cfg` uses the safe implementation settings and
  negates a reachable successful upload + download; exit 12 must name
  `NotSuccessfulTransfer`. This is a reachability witness, not a defect.
- `AmbiguousEffectWitness.cfg` also uses safe settings and must violate
  `NotAmbiguousEffect`: client error + readable storage effect + retired handle
  is intentionally reachable. This is not permission to replay the operation.

All configurations are registered, all endpoint/client lifecycle methods and the
transfer record have source anchors, and the ordinary mandatory lint detects
semantic drift. The discovered contract includes mutations of close admission,
remove-before-close, finally-discard, client cancellation and exact expiry, each
required to change the reviewed digest. New controls are independently named;
parse/runtime errors or a different invariant are not accepted as negative proof.

Run the ordinary project gate and the additional formal gate from the root:

```sh
uv run --no-sync python scripts/check_project.py
uv run --no-sync python scripts/check_project.py --formal-only
```

Results and exact input hashes are preserved by the normal runner in unique
output directories. No threshold, skip, retry-to-green, old CI rerun, or external
model/embedding/judge call is part of this audit.
