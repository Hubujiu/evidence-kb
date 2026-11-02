# Agent-console draft ownership — bounded model

## Scope and source map

`AgentConsoleDraft.tla` models the **question textarea only** in
`kb_platform/agent_console.mjs`, extracted from the prior inline script in
`agent_console.html`. It is a manually reviewed abstraction, not an automatic
JavaScript refinement proof. Whole-module drift hashes, syntax checks, this
model, and actual controlled-browser regression evidence establish different
facts; none substitutes for the others.

| Model boundary | Current source boundary |
|---|---|
| `Submit` | `send.onclick` captures `submittedDraft = draftOwner` before the first possible `await create()` and captures the submitted message. `work` serializes sends, but does not disable the question textarea. |
| `Input(value)` | `question.oninput` allocates a fresh opaque object for **every actual input event**, even if its final text equals the prior/submitted text. |
| `CreateResponse` | `create()` awaits session creation, then installs the session and increments `uiRevision`. This does not replace `draftOwner`. |
| `CreateRefreshResponse` | The separate `await loadSessions()` inside `create()` finishes before the send proceeds. The late-capture negative deliberately captures ownership here instead of at `Submit`. |
| `TurnResponse` | The submitted turn's `await api(.../turns)` completes. |
| `SessionResponse` | The separate subsequent session-refresh `await api(.../sessions/id)` completes. |
| `ClearCommit` | Immediately before clearing, compare captured `submittedDraft` to current `draftOwner`. A matching owner permits `clearDraft()`: explicitly rotate ownership and synchronously assign `question.value = ''`. |
| `ResetScope` | `resetScope()` calls `clearDraft()`, explicitly invalidating draft ownership and clearing the field. |
| `Reject` | An awaited operation rejects or is cancelled as stale, so control leaves the send without reaching its final clear. |

`submitOwner` is a ghost record of the owner **at submission**, independent of
`capturedOwner`, which represents the implementation's owner capture. This is
necessary to detect capture-after-create: comparing a late capture to itself
would otherwise misleadingly pass. `lastCommit` records the question, true
submitted owner, and current owner **at mutation time**, before `clearDraft`
rotates ownership.

The integer draft names are finite labels for distinct opaque objects, not an
implementation generation counter; they are never reused. Up to two input
events, one reset, and one successful clear give names 0–4. Text has three
representatives: submitted text, different text, and empty text. Delete then
retype is two input events and receives two fresh owners. A same-value
replacement event also gets a new owner. Assigning `.value` programmatically
is not itself an input event; `clearDraft` invalidates explicitly.

`scope` abstracts reset invalidation, not `stateKey()` verbatim. `uiRevision`
separately records the legitimate revision increment from implicit creation,
so unchanged implicit first-session drafts remain eligible to clear. API
fetch/body substeps within each call are abstracted to that call's completion;
**create, create-list-refresh, turn, session-refresh, and final clear are not
collapsed into one action**. Input/reset events are allowed only at the four
pending waits. There is no ordinary user task between `SessionResponse` and
its immediate `ClearCommit` Promise continuation. Reset is a conservative
environmental invalidation action; this does not claim that disabled controls
can be clicked while `work` is busy.

## Properties and negative controls

The positive config checks:

- `TypeOK` — the declared finite domains and at most two input events.
- `NoStaleDraftClear` — a clear's true submitted owner equals the draft owner
  at that clear, with no intervening reset scope.
- `NoFailureClear` — failed/discarded sends never reach the clear commit.
- `CurrentDraftClears` — at a completed clear decision, an unchanged submitted
  owner really clears; always-discard is not accepted.
- `NewerDraftPreserved` — a completed send does not change a newer draft.

Independent named controls each check exactly `NoStaleDraftClear`:

| Config | Deliberately broken behavior |
|---|---|
| `LegacyUnconditionalClearNegative.cfg` | Clear every completed send regardless of later input. |
| `TextEqualityOwnershipNegative.cfg` | Use string equality instead of input-event ownership. The shortest counterexample is a same-text replacement event; delete/retype with two fresh owners is also in the bound. |
| `LateDraftCaptureNegative.cfg` | Capture the owner after implicit create/list-refresh rather than before the first await. A newer draft typed during create is incorrectly cleared. |

HTTP success with a returned application `status: error` still completes the
UI send and can clear an unchanged draft; `Reject` denotes an exception/stale
cancellation, not that application status.

## Executed evidence and non-vacuity

Final-source direct runs used Java 21 and pinned TLC 2.19 / release 1.7.4,
one worker and a 128 MiB heap. The positive run exited 0 after **1,932 generated,
1,278 distinct, 0 queued states**. Each of the three negative controls exited
12 with exactly `Invariant NoStaleDraftClear is violated.`; parse/type/runtime
errors were not accepted as negative-control success.

The positive reachable-state dump was parsed and explicit terminal witness
states were verified, not inferred from a clean invariant run:

| Witness | Matching reachable states |
|---|---:|
| Unchanged explicit-session draft actually clears | 1 |
| Unchanged implicit first-session draft clears despite create revision | 1 |
| Different draft during turn / session-refresh remains | 2 / 2 |
| Different draft during create / create-list-refresh remains | 1 / 1 |
| Delete then retype identical submitted text remains, with two input events | 13 |
| Same-value input event remains a distinct owner | 2 |
| Reset cancellation never reaches clear | 150 |
| Rejected send retains different draft | 49 |

Local artifacts are under `.local/browser-owner-models/final-v3/`:
`runs.json`, `AgentConsoleDraft/tlc.log`, `AgentConsoleDraft/reachable.dump`,
`witnesses.json`, and individual sampled witness states under `witnesses/`.
Earlier local iterations are not evidence for these final sources.

Reproduce from the repository root (use a new output directory):

```sh
out=.local/draft-model-new
mkdir -p "$out"
java -XX:+UseParallelGC -Xmx128m -cp /tmp/evidence-tla-tools/tla2tools-1.7.4.jar \
  tlc2.TLC -workers 1 -metadir "$out/states" -dump "$out/reachable" \
  -config formal/agent_console_draft/AgentConsoleDraft.cfg \
  formal/agent_console_draft/AgentConsoleDraft.tla
```

For each negative, substitute its exact config and use a fresh metadir; require
exit 12 **and the named invariant violation**, not merely nonzero exit.

## Bounds and exclusions

One serialized send; explicit/implicit initial session; four pending awaited
phases; at most two input events; one scope reset; three text representatives.
No fairness or temporal property is configured; network silence is allowed.
**No liveness, eventual clearing, or all-browser-state guarantee is claimed.**
Deadlock checking is disabled because settled finite scenarios terminate.

Not covered: repeated sends beyond this one-shot bound, Unicode/whitespace and
IME event details, programmatic third-party textarea edits without the owner
protocol, all other agent scope/status/session-panel/export/citation/download
async gaps, DOM exceptions, browser process exit, or backend request execution,
authorization, transaction, DB, and cancellation effects. A discarded UI result
does not prove that its server-side turn or session creation did not happen.
The existing citation/object-URL model is independent and unchanged. Actual
browser baseline/fixed evidence must be reported separately from these model
results; this model alone does not close the wider state-owner inventory.
