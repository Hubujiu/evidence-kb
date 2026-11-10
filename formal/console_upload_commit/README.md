# Console upload final-commit ownership — bounded model

## Scope and source map

`ConsoleUploadCommit.tla` covers the successful upload's final `ingest` and
message writes in `kb_platform/console.mjs`, competing with the **normal
Create → refresh → select new knowledge base → clearView** path. It is a
manually reviewed abstraction, not an automatic JavaScript refinement proof.
No whole-file hash is treated as a proof of source correspondence.

| Model boundary | Current source boundary |
|---|---|
| `StartUpload` | Upload handler captures `stamp = currentScope()` before `await json(scope()+'/sources', ...)`. |
| `UploadHeaders` | `request` receives headers and checks the original scope before body consumption. |
| `UploadBody` / `BothBodies` | Already-pending `response.json()` body promise(s) fulfill; their reactions enter the FIFO microtask queue. |
| `UploadInnerCheck` | The existing `current(stamp)` **inside `json`** runs after its body await, then resolves the outer upload wait. This queues, but does not execute, the caller's DOM commit. |
| `UploadCommit` | The caller's new `current(stamp)` runs adjacent to the synchronous `ingest.textContent` and message writes. Check and writes are one atomic synchronous action. |
| `StartCreate` / `CreateResponse` | A concurrent Create action receives the new knowledge-base identity and starts its separate `await refresh(kb.kb_id)`. |
| `RefreshBody` / `RefreshInnerCheck` | The list body's completion and the inner `json` scope check are separate from the `refresh` caller continuation. |
| `RefreshSelect` | The queued `refresh` continuation replaces the options and selects the new knowledge base. This changes `currentScope()` before Create's own next continuation. |
| `CreateClear` | The outer Create continuation invokes `clearView()` and writes its success message. `clearView` does **not** clear `ingest`. |

A captured stamp explicitly contains `{viewRevision, kb, token}`. This bounded
scenario has old/next knowledge bases, revision 0/1, and one symbolic token
representative. The comparison is of the **entire record**, not just the kb.
No numeric-revision overflow or owner reuse occurs within the bound. Independent
token changes and more than one revision transition are outside this model.
`lastCommit` records the full owner and scope **at mutation time**: an earlier
valid rendering that remains visible after a later scope change is not falsely
reported as a newly stale write.

## The Promise boundary is explicit

`microtasks` is a FIFO sequence. Every ordinary task transition requires an
empty microtask queue. There is **no artificial user input task interleaved
while Promise reactions drain**. The finite `BothBodies` action permits both
already-pending list/upload bodies to resolve in the same delivery task, in
either order. This directly corresponds to the controlled-browser fixture's
list-then-upload resolution, without modifying the product's Create or refresh
control flow.

The verified negative trace is:

1. Start upload and reach its body wait; start Create and reach its list wait.
2. Fulfill list then upload bodies: queue `[refresh-inner, upload-inner]`.
3. List inner check succeeds: queue `[upload-inner, refresh-select]`.
4. Upload inner check succeeds: queue `[refresh-select, upload-commit]`.
5. `refresh` selects the new kb: queue `[upload-commit, create-clear]`.
6. Without the final guard, old upload writes under the new kb.

Therefore the existing post-body check inside `json` is not atomic with the
caller write. Cancellation alone cannot substitute for the final owner check.
The model abstracts Create's earlier network/parsing detail and successful
header-processing drains; it does not purport to model all Promise machinery.

## Properties, negative control, and observed witnesses

The positive config checks `TypeOK`, `NoStaleUploadCommit`,
`CurrentUploadRenders`, and `NoRejectedUploadWrite`. The named negative
`MissingFinalUploadGuardNegative.cfg` disables only the final upload guard;
all earlier checks and the FIFO queue remain active. It must exit 12 with
exactly `Invariant NoStaleUploadCommit is violated.`

Final-source direct runs with Java 21 and pinned TLC 2.19 / release 1.7.4,
one worker and a 128 MiB heap:

- Positive: exit 0, **67 generated / 52 distinct / 0 queued states**.
- Negative: exit 12, named `NoStaleUploadCommit` violation; the exact FIFO action
  sequence above was also checked against the actual TLC counterexample.
- Parsed positive dump: one current-upload terminal rendering witness; two
  stale-final-commit rejection states **after the inner check had passed**;
  one rejection before the inner check; one valid upload completion followed
  by successful Create. Thus neither always-discard nor start-only vacuity
  explains the positive result.

Local artifacts: `.local/browser-owner-models/final-v3/runs.json`,
`ConsoleUploadCommit/tlc.log`, `ConsoleUploadCommit/reachable.dump`,
`MissingFinalUploadGuardNegative/tlc.log`, `witnesses.json`, and sampled states
under `witnesses/`. Earlier local iterations are superseded.

Reproduce from the repository root with a new directory:

```sh
out=.local/upload-model-new
mkdir -p "$out"
java -XX:+UseParallelGC -Xmx128m -cp /tmp/evidence-tla-tools/tla2tools-1.7.4.jar \
  tlc2.TLC -workers 1 -metadir "$out/states" -dump "$out/reachable" \
  -config formal/console_upload_commit/ConsoleUploadCommit.cfg \
  formal/console_upload_commit/ConsoleUploadCommit.tla
```

Substitute the negative config in a separate output directory and verify both
exit 12 and its exact named invariant. A parse/type error is not a successful
negative control.

## Bounds and remaining obligations

One upload, one Create, one refresh, two kb identities, two revision values,
one token representative, and at most two queued microtasks. No fairness or
temporal property is configured; no eventual response/render or liveness claim
is made. Finite settled runs intentionally terminate, so deadlock checking is
disabled.

This does **not** establish correctness of other refresh/search/connect/publish/
download/status async gaps, the agent console, general multi-operation scope
handling, more than one Create/upload, token changes, object-URL timers, DOM
exceptions, or browser/OS lifetime. The existing citation/object-URL model stays
independent and untouched. Upload/backend authorization, actual server mutation,
DB transactions and external effects are outside this UI model; suppressing a
stale DOM write does not roll back ingestion. Actual browser baseline and fixed
results are separate evidence. This bounded owner does not close the remaining
all-state verification inventory.
