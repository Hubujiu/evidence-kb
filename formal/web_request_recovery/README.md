# React request recovery: bounded state model

This is a manually reviewed abstraction of `web/src/turn-request.ts` and the send/recovery orchestration in `web/src/main.tsx`. It is not automatic TypeScript refinement, a backend delivery proof, or a complete browser-state proof.

## Correspondence

| Model action | Implementation boundary |
| --- | --- |
| `Start` | Capture request UUID, identity, scope, session and draft ownership before any turn POST. |
| `CreateSuccess` / `CreateFailure` | Lazy session creation installs the session or preserves the unsent question. |
| `Post` | Send exactly one turn with the retained request identity. |
| `LoseResponse` | Enter `unconfirmed` without inferring whether the provider ran. |
| `Check` / `Observe` | Manual GET of the original request; all unresolved dispositions remain blocked. |
| `Receive` | Record a known result independently of any following view refresh; clear only the submitted draft and render only in its owning view. |
| `Refresh` / `RefreshFailure` / `Synced` | Synchronize authoritative session revision; a failed GET preserves the already received result. |
| `NextTurn` | A subsequent send becomes available only after the original result and session projection are settled. |
| `Park` / `NextDistinct` | Explicitly retain a manual-resolution attempt while allowing a fresh request in a distinct session; the original identity remains readable. |
| `Input` / `Navigate` | A later draft or different displayed conversation must not receive an old completion's destructive effect. |

`known` is independent evidence that a response has been received; it is never reconstructed from the current UI phase. `posts` counts attempted HTTP turn submissions, while `lastReadId` records recovery identity. Neither variable is a token-billing assertion. A business error or authorized redaction is a known result, not a transport failure.

## Invariants and controls

The positive configuration checks typed finite domains, identity capture before POST, exact original identity for recovery reads, no next send while the original result remains unknown, no downgrade of known results, draft ownership, preservation on creation failure, and current-view rendering.

| Negative configuration | Deliberate fault | Required violation |
| --- | --- | --- |
| `UnknownReleaseNegative.cfg` | Treat unresolved query outcomes as complete | `NoUnconfirmedNextSend` |
| `RefreshDowngradeNegative.cfg` | Convert a known result into unknown after GET failure | `KnownResultNeverUnknown` |
| `StaleDraftNegative.cfg` | Clear a later draft unconditionally | `NoStaleDraftClear` |
| `StaleViewNegative.cfg` | Render an old completion into a different view | `NoStaleRender` |
| `ParkedSessionNegative.cfg` | Reuse the unresolved original session after parking | `NoParkedSameSessionSend` |

`NoUnconfirmedNextSend` permits a separate fresh session only after explicit parking; it does not equate independent work with resolving or resending the original request. `NoParkedSameSessionSend` checks the new session and request identities, while `RetainedRequestIdentity` and `OriginalRequestReads` retain the old request for inspection.

Pinned TLC 1.7.4 / Java 21 direct execution checked 819 generated and 338 distinct states in the final positive run, exit 0. All five controls exited 12 on their named invariants. An earlier local model iteration failed with an incompletely assigned successor; that failed attempt is retained separately and is not negative-control evidence. The intermediate model without parking checked 641 generated and 278 distinct states; those counts are historical. Detailed local and CI status is reported in the feature validation record.

## Bounds and exclusions

One original logical request with explicit or implicit session creation; at most two manual recovery reads; one draft replacement; one view change; one retained record; one subsequent send attempt either in the settled original session or a distinct session. The second request is modeled only as its admitted send identity, not as a second full active lifecycle. There is no fairness assumption or eventual-network-success claim. The endpoint response abstraction admits arbitrary observed statuses but does not claim that an interrupted backend request can become completed; the browser fixture for permanent interruption never makes that transition. Known result values are abstracted, not compared for answer correctness.

Exact pre-handler delivery rejection, ambiguous one-call `model_sent: false`, identity reset, mismatched response IDs, multiple retained records, routing to active versus retained records, and unlocking the original session after explicitly opening a confirmed retained result are direct reducer/API browser contract concerns outside this bounded model. Whole TypeScript module hashes force review of changes but do not prove the source implements every model action. Existing `OperationOwner` contracts remain necessary for identity/scope/view ABA and stale `finally` behavior.

Not covered: backend leases, storage commits, billing/usage, authorization enforcement, provider effects, page reload persistence, DOM allocation exceptions, arbitrary concurrent browser callbacks or unbounded attempts. Browser cancellation does not imply server rollback. The full checker and actual React browser suite remain required separately.
