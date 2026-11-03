# Auxiliary OCR upload ownership

`AuxiliaryModelEndpoint` owns the RPC upload map, not OCR page recovery or the
provider's request ledger. Upload IDs identify one endpoint-local staging object.
The public contract provides no actor field and does not make the token a durable
idempotency key. Calls are admitted by the containing plugin capability and host
lease.

## Protocol and ownership boundaries

1. `ocr_begin` checks provider availability, removes expired staging objects, and
   reserves one of eight slots. An accepted upload expires after 900 seconds;
   the implementation expires it only when `expires < monotonic()`.
2. `ocr_append` validates Base64, chunk size, exact next offset, and declared total
   size while holding the upload lock. A rejected append does not advance bytes.
3. `ocr` validates complete length and SHA256 before removing the upload from the
   map under that same lock. Incomplete or corrupt input remains cancellable and
   never invokes the provider.
4. The consumed bytes are passed to `complete_json` after releasing the lock.
   Concurrent consumption or cancellation of that token cannot recreate it.
   Provider errors, invalid results, and `BaseException` do not restore the upload
   or automatically invoke the provider again. This is single consumption, not
   proof that a failed provider request was unsent.
5. `ocr_cancel` and direct `close` release only pending staging objects. They do
   not undo a consumed call. The endpoint is not the owner of shutdown admission.

`knowledge_capabilities.build_knowledge_application` registers
`selected.capability_endpoint.close` with `ctx.defer`. `ContractContext.provide`
wraps the endpoint in `ServiceEndpoint`; `Dependency` and `PluginHost._invoke`
hold an invocation lease through completion and release it in `finally`.
`PluginHost.close` first drains admission and reports `scope_busy` while leases
remain. Cleanup runs after admitted calls settle; new calls are rejected. A raw
endpoint retained outside this lifecycle is not a supported business-call path,
so lack of an endpoint `_closed` flag is not evidence of an admission defect.

## Mature protocol references

- [tus 1.0.0 core protocol](https://tus.io/protocols/resumable-upload#core-protocol)
  separates upload identity from exact offsets and requires an offset mismatch to
  leave the resource unchanged. Its expiration and checksum extensions are useful
  reference boundaries, not a claim that this JSON RPC endpoint implements tus.
- [S3 multipart upload](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html)
  uses an upload ID across parts/completion/abort and checks full-object integrity.
  It also distinguishes aborting staged work from already-running part requests.
  This endpoint does not adopt S3's out-of-order parts or retry behavior.
- [Python Executor shutdown](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Executor.shutdown)
  distinguishes stopping admission and cancelling pending work from cancelling
  running work. The repository enforces its own stronger drain-before-cleanup
  boundary using plugin leases rather than an executor.

## Deterministic executable contracts

`kb_platform/rag_testing/auxiliary_ocr_transfer_contracts.py` uses the real endpoint,
JSON `ServiceEndpoint`, temporary-file bytes, and a synthetic provider. It covers
two interleaved uploads, capacity, exact expiry boundary, stale tokens, rejected
appends, incomplete/corrupt images, single-consumer races, provider errors and
cancellation, invalid output, and direct pending-object cleanup. A separate real
`PluginHost` contract establishes drain rejection and deferred cleanup while the
synthetic provider is blocked. These are component/protocol contracts, not image
recognition quality or external-provider integration tests.

The finite protocol excludes UUID/hash collisions, arbitrary payload sizes,
provider-internal retry and billing, scheduling fairness, and external model
correctness. Cancelling an upload is not a cancellation or rollback protocol for
an already-consumed invocation.
