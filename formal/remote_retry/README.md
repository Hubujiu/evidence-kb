# Remote retry, format/auth continuation and real work ownership

Base: local `54a2686ebde6e2b88662e3d695ffa53be3eb36e9`, published PR42
`6650d89c471ac1b9aca3941da7bfc24737405832`; both have tree
`f8f775450be090b339e156d72e233d3546336be0`. This independent feature changes
models, mandatory structural checks and contracts only. It does not replace
PR33 circuit generation, PR36 receipt ownership or PR42 post-receipt admission
fixes. All production owner files are byte-identical to the approved base.
**No new product defect is established by this batch.**

## Mature implementation and accounting boundaries

The locked dependencies reviewed are OpenAI 2.54.0, google-auth 2.62.0,
HTTPX 0.28.1 and Tenacity 9.2.1. The runtime owns model retries rather than
stacking independent SDK retries: `luna_sdk.chat_response` supplies
`max_retries=0`, uses the caller's HTTP client, returns received error responses,
and unwraps connection-error causes.
[Official pinned OpenAI source](https://github.com/openai/openai-python/blob/v2.54.0/src/openai/_base_client.py)
and [pinned Tenacity stop/wait implementation](https://github.com/jd/tenacity/blob/9.2.1/tenacity/__init__.py)
were reviewed. Installed locked-source hashes are retained in the evidence.
An attempted google-auth 2.62.0 GitHub tag fetch failed; the reviewed exact
version is the installed locked source, not a different-version web page.

A real offline execution of google-auth `jwt_grant` invokes its injected
request callback three times for 503, 503, 200, with planned waits 1 and 2.
That is one logical credential refresh and **zero model dispatches**. Callback
invocations are not a guarantee about OAuth wire transmissions: requests,
redirects, OS behavior and future SDK versions are separate boundaries.
No credentials or external OAuth/model requests are used by these fixtures.

The actual owners deliberately have different counters:

| Owner | State and observed boundary |
| --- | --- |
| `_Transport._post` | Global trace attempt, per-post transport index and planned wait, prior physical ID/reason. A refused preflight removes the provisional attempt count. |
| `LunaClient._complete` | At most two permitted format stages with one logical ID; per-post retry/wait resets, global attempt does not. Original user is retained; graph/raptor correction adds a purpose-specific strict wire schema. |
| `_GoogleRequest` / Google `_post` | One shared transport retry budget across the 401 continuation. Auth stage and the one-attempt authentication-retry flag are distinct from model attempts. |
| `_embed` / `_vertex_headers` | Auth stage promotes only after headers return. Failed refresh can have one actual refresh invocation but zero completed auth stages. Concurrent stale-token callers may adopt the current token instead of refreshing again. |
| `_GoogleRequestPolicy` | Lock-protected nondecreasing cooldown and next-slot reservation; sleep outside the lock; recheck on wake; actual elapsed wait recorded in finally, including refusal/oversleep. |
| `_FifoSlots` | FIFO enqueue/claim, exceptional waiter removal, capacity retained until real work context exit. Cancellation does not mean an arbitrary blocked function has stopped. |

Durable receipt rows contain global attempt and retry links. Format/transport
stage counters are observed in provider events, not invented in receipt rows.
A received HTTP ERROR does not prove rollback, no side effect, or zero usage.
UNKNOWN is never blindly retried. Usage not observed remains reserved/unknown.

## Three separate bounded safety models

These are not the full product of all concurrent owners or a compiler-level
refinement proof. No fairness/liveness is asserted. Clock/lock actions are
abstractions, and each model/config is bound by the mandatory registry.

- **RemoteRetryFormat:** one logical request, two format stages and two
  transport attempts per stage; abstract time 0..5, wait cap 2 per stage,
  HTTP hints 0/1/3. Actual provider-work bit and observed receipt state are
  separate. Physical identities are abstract unique labels, not a wire
  monotonic-ID requirement. No UNKNOWN replay, identity/link loss, third
  correction, or wait below a received hint. Invalid HTTP envelopes terminate;
  malformed allowed completion contents may correct. Successful receipt
  history is retained even when later format validation fails.
- **GoogleRetryAuth:** one logical query with an initially valid credential,
  one transport retry and one auth continuation. Model dispatches, completed
  auth stages, actual credential refresh invocations and bounded SDK token
  request callbacks are distinct. Current-token adoption is an environment
  action representing another caller's completed refresh; auth-lock concurrency
  is exercised in the real Event regression, not proved as a combined TLA
  interleaving. Failed refresh does not promote a completed stage.
- **SharedGoogleWaitSlots:** two requests with separate one-slot and two-slot profiles, ticks 0..2, wait cap 2,
  cooldown hints 1/3. FIFO claims, interrupted waiters, rate reservation and
  actual active work are distinct. A release cannot precede actual settlement.
  A reservation must satisfy the cooldown observed at reservation time. This
  intentionally does not assert spacing of later actual wire sends.

Eleven deliberately **seeded** policy mutations must reach their named invariant
failures: unknown replay, duplicate physical identity, third format stage,
hint clamping, reset of auth's retry budget, repeated auth continuation,
counting OAuth callbacks as model sends, premature slot release, FIFO bypass
ignored cooldown and over-capacity admission. None is labelled a discovered current production bug.
Five additional negative assertions are **reachability witnesses, not bugs**:
composed retry/format success, composed Google auth/retry success, complete
slot drain, a real active one-slot owner blocking its queued competitor, and distinct rate reservations with equal abstract send times.
The last witness prevents an unjustified strict-wire-spacing claim.

The first Google model imposed an invalid invariant equating an unfinished or
failed refresh invocation with completed auth-stage promotion. Its genuine
counterexample is retained. The actual failed-refresh regression confirms the
correct distinction; the revised model bounds SDK callbacks separately. The
first shared-policy five-tick exploration was interrupted with exit 130 after
exceeding practical targeted runtime; it is not a successful proof. Its input
and log are retained. The final smaller two-tick bound is explicit and uses the
same safety invariants; no verdict is transferred from the interrupted run.

## Actual deterministic contracts and mandatory guards

Real CapturedLuna + fixed SDK + MockTransport + BudgetLedger exercises
503 -> malformed completion -> 429 -> corrected completion: four linked
physical identities, global attempts 1..4, format stages 1/1/2/2, transport
indices 1/2/1/2, and two unknown-usage error responses. Two .5 sleeps under
separate per-post .75 caps total 1.0 for the logical call: there is no claimed
global wait cap across correction. ReadTimeout after a fixture effect leaves
one UNKNOWN, and a received 429 with a fixture effect plus Retry-After 100
retains ERROR/unknown usage without a second send or rollback assertion.

Real Google query/receipt/ledger paths exercise 401/503 in both orders,
exhaustion after auth, second 401, and credential refresh failure. Controlled
credential refresh is a stub, not a real OAuth call. The new fixture explicitly
configures `trust_env=False` and a MockTransport; no environment is cleared or
existing failing full-gate contract is altered. Real auth and FIFO locks use
Events, not sleeps, to check current-token adoption, ordering, interruption
and actual slot release. Fake monotonic time exercises concurrent cooldown
extension, unclamped large hints and actual oversleep accounting. Oversleep
beyond an admission cap is recorded rather than falsely presented as a hard
real-time deadline defect.

`remote_retry_lint.py` is called by `registry_violations`, so it runs in the
ordinary mandatory formal-lint and formal-only gates. It checks concrete AST
structure at retry/counter, correction, credential, cooldown, FIFO and SDK
boundaries; hashes alone cannot satisfy it. Contracts reject 22 seeded source
mutations and eight whole-registry hash-refresh mutants. Existing PR42
mandatory post-receipt admission checks remain unchanged and active.

Initial probe KeyError (format fields looked up in the wrong receipt record)
is retained as a probe error, not a product defect. There are zero new external
model, embedding, judge or OAuth calls. Remaining obligations include arbitrary
provider duration/usage, unbounded concurrency/time, SDK transport internals,
crashes and full cross-owner composition.

## One-slot coverage correction

The first frozen WIP `4e9354a` / published `509dd6c` modeled two callers and
two slots. Its SlotCapacity invariant could not challenge over-admission:
two unique owners cannot exceed a capacity of two. That earlier profile is
retained, not retrospectively presented as an over-admission mutation check.
The follow-up adds a two-caller/one-slot positive profile, a named capacity-
bypass seeded counterexample, and an active-owner/queued-competitor reachability
witness. The existing real Event-based one-slot protocol test already exercises
blocking and true release; a new source mutant removes the zero-available guard
and remains rejected even after all source hashes are refreshed. This corrects
a finite-model coverage weakness, not a production bug.
