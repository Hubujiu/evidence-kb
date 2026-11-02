# Browser draft and final-mutation ownership: reproduction only

This baseline adds deterministic browser regressions, not a production fix.
Neither candidate is claimed as browser-confirmed before normal CI produces
the actual assertions. No local Chromium run or download is attempted.

## Immutable dependencies

The local combination commit `77952a91db027fd7dae5c56b086b7d8167919924`
merges the complete PR38 citation-owner head with the complete PR31 failure and
budget-status head. Its tree is `305f77b934dfd65432a7118ac681103857eba62f`.
The corresponding remote dependency is `deps/formal-browser-llm` at
`0007559be4fb377eeff644a4c1dbb1c7ca7fb162`, with the same tree.
An automatic conflict-free merge is not an acceptance result.

The production `agent_console.html`, `console.html` and `console.mjs` remain
byte-identical to that combination. Existing `knowledge.e2e.js` and all seven
`console-state.e2e.js` cases are unchanged. The exact file bindings are in
`browser-state-baseline-v1.json`.

The raw combination's formal lint failed with eleven previously reviewed PR31
source drifts and the extracted reviewed-predecessor projection candidate.
Only this feature layer applies the explicit old/new source review in
`browser-state-pr31-source-review.json`, including three extracted/new anchors.
No family is promoted by that dependency review, and no hash is a proof.

## Mature boundaries

- [React's stale-effect example](https://react.dev/reference/react/useEffect#fetching-data-with-effects)
  checks the request owner next to the actual state update, after the awaited
  work, rather than treating an earlier check as permanent authority.
- [MDN input events](https://developer.mozilla.org/en-US/docs/Web/API/Element/input_event)
  distinguish user edits from programmatic assignment. A user can delete and
  retype the same text; equality with the submitted string does not identify
  the same draft.
- [MDN microtasks](https://developer.mozilla.org/en-US/docs/Web/API/HTML_DOM_API/Microtask_guide)
  describes promise reactions and task ordering. Normal user input is a task;
  it cannot simply interrupt a draining microtask queue. The scope candidate
  therefore uses two existing asynchronous actions, not a fabricated token
  event injected between promise callbacks.

No upstream code or dependency is added.

## Draft candidate

Agent console disables buttons and sidebar controls while sending, but leaves
the question textarea editable. Its successful turn/session refresh path
unconditionally assigns an empty question value. The browser cases use actual
DOM input and native promise-backed synthetic HTTP bodies to hold:

1. The turn response while a different next draft is entered.
2. The turn response while the question is cleared and the identical submitted
   text is retyped. A future string-equality-only guard must not pass this case.
3. The follow-up session read while a new draft is entered.
4. Implicit first-session creation while a new draft is entered, before the
   submitted turn request is dispatched.
5. An unchanged submitted draft, which must still clear after success.

The positive setup also renders PR31's explicit unknown/unavailable budget
projection. The fixture is a frontend component scenario, not budget admission
or backend-consistency evidence. It makes zero provider calls.

## Scope candidate and controls

The document console's `json()` checks its captured scope after reading the
body, but callers perform later awaited success mutations. Not every such
source-level gap is necessarily reachable from ordinary UI actions.

The candidate uses actual Upload and Create buttons while they are independently
enabled. Both requests start in the old scope. Create completes and begins its
existing list refresh. After both body readers are waiting, the fixture resolves
the list body and then the upload body in one batch. It does not replace any
application function, inject a scope event, or use a custom thenable. The normal
refresh selects the newly created scope through the application's own code.

A pass-through wrapper on the real ingest element's native `Node.textContent`
setter records the current scope synchronously at each actual DOM mutation.
It calls the original setter and does not add an asynchronous transition. The
assertion rejects a new READY mutation from an old-scope upload after the UI has
selected another scope. This distinguishes a late write from merely retaining
some previously rendered text.

Two controls require a current upload to render normally, and require Create's
later clear to survive a concurrently completed older search. The search case
is expected to pass; it does not establish that all success callbacks are safe.
The already fixed citation selection, final error-owner check and object-URL
ownership are unchanged and retain their seven original browser cases.

## Normal CI evidence contract

The existing Rust-host HTTP browser harness first verifies exact public asset
bytes and response headers for `/`, `/console.mjs`, and `/agent`. It keeps the
two existing knowledge acceptance cases, then the seven citation-owner cases,
and then both new suites. New suite failures do not suppress the other new
suite. Every case is mandatory, no retries/skips are accepted, artifacts stay
inside the framework's allowed `e2e/.artifacts` tree and are copied out even on
failure. The policy requires zero model calls and zero model tokens. The collector
reads each suite's complete raw usage report even after ordinary assertion failure.
It reports observed zero only when the report has the expected discovered-case
count, matching normal exit/status, no infrastructure errors, and zero integer
usage counters. Missing, partial, malformed or infrastructure-failed observations
remain UNKNOWN/NOT_RUN with `new_model_calls: null`; policy is not observation.
Both suites still run, and any failure still exits nonzero.

Expected candidate outcomes are four draft assertion failures and possibly
the upload/scope assertion failure, with positive setup and controls passing.
A missing asset, fixture setup error, timeout or malformed report is not proof
of either bug. Actual CI artifacts must be inspected before a correction is
implemented or a candidate is called confirmed. Browser status is **NOT_RUN
locally**; no local sandbox, profile, proxy or download workaround is attempted.

Draft ownership and success-mutation scope ownership remain unmodeled in this
reproduction head. Any subsequent correction must have its own bounded model,
named negative controls, source review and mandatory lint, preserving PR31's
budget semantics and PR38's already checked citation behavior. No visual design
or new frontend feature is part of this work.
