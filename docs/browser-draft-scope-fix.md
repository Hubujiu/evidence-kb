# Browser draft and upload final-commit ownership

## Actual baseline, before changing production

PR52's first run `41527311466` reached the upload race assertion but all five
draft cases stopped at setup: the budget disclosure was closed. Those draft
assertions were **NOT_REACHED**, not bug evidence. The fixture-only successor
opens the existing summary through a real tap and retains both visible budget
assertions, all cases, timeouts and zero-retry policy.

Corrected baseline remote `55315bd447e81fe510a73c9486f6e9e7ef2354a6`
(local `638ad262fc5b2616b21e81629ed7b294a8056345`), tree
`96c80b498956186593137f717c8c457e7d971047`, was tested by normal CI run
`41533302556`. Its merge commit `d95aa49b89382f1246a6f3a6ea4d7b8107dd0a6f`
has that exact tree. Production remained byte-identical to the full PR31+PR38
dependency combination. Actual Chromium was **156.0.8078.4**.

| Suite | Actual outcome |
|---|---|
| Existing knowledge acceptance | 2 PASS |
| Existing citation-owner regression | 7 PASS |
| Draft ownership | 4 intended final-value FAIL, 1 positive PASS |
| Upload/scope ownership | 1 intended stale-write FAIL, 2 controls PASS |

All four draft failures reached successful sending, actual textarea edits,
response/session completion and answer rendering, then observed `""` instead
of the newer draft. They cover turn wait, identical delete/retype, session-read
wait and implicit-session creation. The upload failure recorded an actual new
READY write under scope `next` from the old upload, not merely retained text.
The normal concurrent Create/refresh path changed the scope; no fabricated user
task interrupted a draining Promise microtask queue.

Raw reports, JUnit and traces agree on all 17 case names and one attempt each;
no skips, flaky cases, retries or infrastructure errors. All four complete
usage observations report zero model calls/tokens. All 11 reported attachment
hashes and sizes were verified. Integration ZIP artifact `13184026126` SHA-256:
`ee3e0c942122d7c8bf2795627f7e9cf76d44f4a971030c425f4590c393de9776`.
The root browser report did not record host exit code on its failure path;
that field remains unobserved, not reconstructed as zero. Independent host
stdout reports completed/stopped. Raw evidence is retained outside source in
`ci-evidence-pr52-disclosure-baseline/`.

## Minimal corrections and mature references

The [React stale-effect example](https://react.dev/reference/react/useEffect#fetching-data-with-effects)
checks ownership at the actual update, after waiting. [MDN input events](https://developer.mozilla.org/en-US/docs/Web/API/Element/input_event)
distinguish user edits from programmatic value assignments. [MDN microtasks](https://developer.mozilla.org/en-US/docs/Web/API/HTML_DOM_API/Microtask_guide)
describes the Promise/task boundary used in the actual upload schedule. No
upstream code, new package, visual redesign or frontend feature is introduced.

- **Draft:** extract the original inline script into the sole mapped
  `agent_console.mjs`. Each input event allocates a fresh opaque identity,
  including retyping the same text. Send captures it **before** implicit create
  can await. Final clear compares that identity in the same synchronous block;
  explicit clearing/reset rotates ownership. An unchanged implicit-session draft
  still clears: session creation's unrelated `uiRevision` is not draft ownership.
- **Upload:** capture the complete existing `{viewRevision, kb, token}` scope
  before waiting; call existing `current(stamp)` immediately before synchronous
  ingest/status writes. The earlier inner JSON check remains. No owner metadata
  is serialized and existing stale-error suppression remains unchanged.

HTML/CSS outside the extracted script is byte-identical. PR31 budget-unknown and
error-response behavior is retained. PR38 citation identities, final error-owner
checks and URL ownership are untouched. `browser-state-fix-v1.json` separates
old/current hashes and explicit reviews; hashes are review-drift evidence only.

## Mandatory checks and model scope

Both entrypoints must use their one mapped `.mjs` script. Standard HTMLParser
placement lint rejects extra/inline scripts, inline event attributes, `srcdoc`
and JavaScript URLs even after refreshing a module hash. Actual Node/V8 module
syntax checks run through the existing unified stage, including a deliberately
invalid syntax negative control. The sealed Python contracts do not spawn Node.
Whole-module normalized SHA bindings require explicit review of source drift.
**These are syntax/placement/review checks, not semantic JavaScript AST proof,
an HTML security sandbox, or automatic TLA refinement.**

The real host-application ASGI path must serve `/agent_console.mjs` publicly with
exact bytes, JavaScript media type, no-store and nosniff while `/identity` remains
protected. Normal CI also checks all four assets through the actual Rust-host
HTTP path before browser cases. Missing/stale assets or wrong headers fail.

Two independent partial models and four named negative controls are registered:

- [Agent draft model](../formal/agent_console_draft/README.md): one send, four
  awaited phases, two input events and one reset. The legacy unconditional clear,
  string-equality ownership and capture-after-create negatives each violate
  `NoStaleDraftClear`.
- [Upload commit model](../formal/console_upload_commit/README.md): one upload
  competing with one Create/refresh; explicit FIFO Promise reactions; final
  guard removal violates `NoStaleUploadCommit` after the inner check succeeds.

Final-source direct TLC runs passed with 1,278 and 52 distinct states. Four
negatives each exited 12 for the exact expected invariant. Parsed state dumps
verified 14 reachability scenarios, including unchanged implicit drafts and
actual current upload rendering; this is not an always-discard proof. Model
READMEs record bounds, source boundaries and exact reproduction instructions.
The separate unified formal gate remains required.

All original 2+7 acceptance/citation cases and all eight baseline cases remain.
One added positive browser case requires an unchanged draft through implicit
session creation to clear, giving **18 mandatory cases**. Usage must be fully
observed at zero, not inferred from policy; missing reports remain UNKNOWN.

Fixed-head actual browser result is **NOT_RUN locally**, awaiting normal CI.
No local browser/download/sandbox workaround is attempted. No paid provider,
embedding or judge calls are made. Ordinary full checks and formal-only results
must be reported for the frozen source, separately from direct targeted runs.

## Explicit remaining boundaries

No proof is claimed for other final success mutations in refresh/search/connect,
status, export, download or agent session panels. The passing search control does
not cover all schedules. Models omit repeated sends/uploads beyond their bounds,
IME and third-party programmatic edits, DOM exceptions, independent token changes
in the upload schedule, numeric revision overflow, browser lifetime and liveness.
External DB consistency, backend authorization at each await/commit, server
cancellation and actual request effects are outside these UI models; suppressing
a stale render does not undo a server action. The broader all-state inventory
remains open.
