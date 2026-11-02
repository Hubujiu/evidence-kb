# Citation view ownership — bounded model

## Scope and source correspondence

`ConsoleCitationView.tla` models the asynchronous citation viewer in
`kb_platform/console.mjs`: `showCitation`, its `json`/`blob` calls and error
projection, and `clearView` invalidation. The same owner originally lived in
`kb_platform/console.html`. This is a manually reviewed abstraction, **not** an
automatic JavaScript-to-TLA+ refinement proof. Module hashes, JavaScript syntax
checks and HTML script-placement checks establish different, narrower facts.

The two request identities A/B are **selection generations**, not reusable
citation IDs. Each is selected once. Their metadata, preview and download
identity are distinguishable. Scope generation is 0 or 1, with at most one
switch; a scope represents the complete view revision, knowledge-base and token
stamp. Actual IDs, credentials and document bytes are abstracted away.

| Model action/state | Source boundary |
|---|---|
| `Select` / `metadata-fetch` | User selects a citation; capture scope and selection generation; await metadata fetch. |
| `MetadataHeaders` / `metadata-body` | Fetch resolves; existing scope validation precedes asynchronous JSON body reading. |
| `MetadataBody` / `preview-fetch` | Validate current scope/selection, then synchronously replace citation metadata. PDF preview fetch starts separately. |
| `PreviewHeaders` / `preview-body` | Preview fetch resolves; scope validation precedes asynchronous blob consumption. |
| `PreviewBody` | Validate again after the body await; only then revoke the previous current URL, allocate the new URL, and insert preview/download controls. |
| `Reject` / `error-ready` | Fetch or body rejects at any of the four pending phases. The propagated error retains the original scope and selection identity while its outer action callback is queued. No status write occurs yet. |
| `Failure` | The outer `action` callback samples the current owner immediately before the synchronous status write. Scope/selection may change between `Reject` and `Failure`; an inner catch-time check is not sufficient. |
| `SwitchScope` | Invalidate all old request stamps, clear rendered state and release the currently owned URL. |

Synchronous DOM and URL mutations between awaits are one atomic transition,
consistent with the JavaScript event loop; the **four awaits are not collapsed
into one action**. Error propagation is also split from its final status commit:
`Reject` and `Failure` are separate transitions, with selection and scope changes
permitted between them. The final owner check and the status mutation share the
same synchronous transition; an earlier check followed by rethrow would not
satisfy this correspondence. Previously rendered content may remain while a newer
selection is still fetching. `lastRender`, `lastRelease` and `lastFailure` are
ghost observations of the scope/selection **at mutation time**, so this permitted
old display is not incorrectly treated as a new stale write.

The model retains the current URL until replacement or scope clearing, even if
metadata replacement already removed its image element. This conservatively
covers the original ownership shape; an implementation that also releases that
URL during a current metadata replacement needs the same current-owner guard.

## Checked properties and negative controls

The positive configuration checks `TypeOK`, `NoStaleRender`,
`NoStaleURLRelease`, `NoStaleFailure`, `CitationIdentityCoherent`,
`CurrentURLLive`, and `AtMostOneLiveURL`.

- `LegacySelectionNegative.cfg`: missing selection guard; `NoStaleRender` fails.
  Existing scope checks remain enabled. Scope equality alone is insufficient.
- `LegacyURLReleaseNegative.cfg`: the same legacy guard mutation, isolating
  `NoStaleURLRelease`. TLC finds A metadata → B metadata → B preview → A late
  preview: A revokes B's URL and inserts A's preview under B's metadata.
- `MissingPreviewBodyGuardNegative.cfg`: disable the post-preview-body guard;
  `CitationIdentityCoherent` fails. A response can pass the header check, then
  arrive after a scope switch. This is a seeded regression of the required
  second check, **not a claim that the baseline lacked its existing scope-body
  check**.
- `StaleFailureNegative.cfg`: obsolete failures write status;
  `NoStaleFailure` fails. Failures are available before headers and while a body
  is pending. Rejection propagation has its own `error-ready` phase, so checking
  only inside an inner catch (before the final status callback) cannot make this
  control pass.

## Bounds, observations and remaining obligations

There are two one-shot selections, two scope generations, four pending phases
and one rejection-queued phase per selection, and two symbolic URLs (`unallocated`, `live`, `revoked`). No
fairness assumptions or temporal properties are configured; permanent network
silence is allowed, and **no liveness guarantee is claimed**. Deadlock checking
is disabled because fully settled finite scenarios intentionally terminate.

A direct local run using pinned TLC 2.19 / release 1.7.4 and Java 21 explored
1,488 generated / 598 distinct / 0 remaining states for the positive model.
A dumped reachable-state set includes 117 states with a completed request, seven
with both requests completed, 84 with a current live URL and 187 with a queued
rejection: the positive check
is not a start-only or always-discard abstraction. All four negative controls
actually exit 12 with their named invariant violations. These are **model
results**, not browser regression evidence; the feature's exact-source browser
baseline/fixed runs must be reported separately. A private constrained TLC
witness also exercises `Select A` → `Reject A` while current → `SwitchScope` →
`Failure A`: the final-guard variant passes, and omitting that final guard
violates `NoStaleFailure`. Thus an earlier successful owner check does not
dominate the later status callback.

Not covered: more than two selections, ABA reuse of generation counters,
non-PDF display, independent original-download URLs/timer reclamation, browser
process exit, memory exhaustion/DOM exceptions, popup/download policy, server
mutation or cancellation semantics, document integrity, authorization beyond
the captured client scope, and the agent-console editable-draft owner. Selection
cancellation is optional cleanup; it does not prove that a server operation did
not run. Review and real controlled-browser ordering tests remain necessary.

The ownership pattern follows the stale-result suppression described in
[React's official data-fetching example](https://react.dev/reference/react/useEffect#fetching-data-with-effects).
[MDN's AbortController documentation](https://developer.mozilla.org/en-US/docs/Web/API/AbortController/abort)
describes fetch/body cancellation, not rollback of server-side effects.
