# ToolExecutor claim, retry and terminal ownership

Base: `c8ef7582f226c07f1c06d3874dba159708ca7fad`. Production change is limited
to `ToolExecutor.execute`. ProviderCircuit, RequestReceipts and remote.py are
unchanged; their independent fixes are not replaced by this branch.

## Mature behavior reviewed before the fix

Pinned `uv.lock` dependency: Tenacity **9.2.1** (sdist SHA-256
`a606b5c808d0cded4a359d5b9932d867ff2a6a6b64d37350260fd01bbdf83839`).
[Official pinned implementation](https://github.com/jd/tenacity/blob/9.2.1/tenacity/__init__.py)
and [retry/stop/wait documentation](https://tenacity.readthedocs.io/en/latest/)
were reviewed on 2026-11-02. `TryAgain` is an intentional explicit retry:
`_begin_iter` bypasses the configured retry predicate, while subsequent actions
still evaluate wait, then stop, before sleeping. This is not a Tenacity defect.
The application must enforce its safety policy at that unbypassable stop point.
Tenacity's `idle_for` counts planned sleep, not operation duration or scheduler
oversleep; the separate monotonic deadline includes elapsed time. No new retry
framework or changed exception hierarchy is introduced.

Reviewed installed source hashes:

| Tenacity file | SHA-256 |
| --- | --- |
| `__init__.py` | `9c050f8f59801a5bfb5a1738dd75d4ee544f368481e068268571c266e2b96577` |
| `stop.py` | `bd4b852e18e886fc271da51760c39fe49ff4174f916f07f3565c280f4f2d2c71` |
| `wait.py` | `fab8c53ae00dda64008a70c03654f22e059f3d697c3c8a6029513910fa7179fc` |

[HTTPX exception taxonomy](https://www.python-httpx.org/exceptions/) supplies
transport classifications, not a guarantee that an HTTP error response undoes
business effects. The existing classifier is unchanged. A read-only declaration
is caller authority, not a proof of provider purity. UNKNOWN may include an
already committed effect and is not retried, even with that declaration.

## Confirmed implementation counterexamples

1. With `read_only=False`, a real temporary SQLite operation commits one insert,
   raises `TryAgain`, and would return on the next invocation. The old executor
   performs **two committed inserts**; the fixed executor performs **one**,
   returns the original ERROR classification and retains its terminal receipt.
   ERROR describes the observed exception; it does **not** assert rollback.
2. The same actual SQLite effect followed by `TryAgain` caused by ReadTimeout
   was also repeated even with UNKNOWN classification. It now remains UNKNOWN
   with one committed insert. Reusing the same request identity does not execute
   either operation again. The test deliberately declares read-only to challenge
   the independent UNKNOWN guard, not to portray that fixture as truly pure.
3. A real public PlatformEndpoint/ServiceEndpoint JSON roundtrip converts a 429
   into RemoteModelError and preserves `retry_after_seconds=100`. The old wait
   inspected only HTTPStatusError headers and retried after a planned .25s
   despite a 2s total-wait budget. The fix incorporates the finite nonnegative
   public hint before stop evaluation: one call, no sleep and no clamping.
   Separate .6s hints cover accumulated planned wait and deadline refusal.

Nonfinite, negative, boolean, nonnumeric and unrepresentable numeric hints are
ignored, retaining normal backoff; they cannot produce an invalid sleep.
HTTP Retry-After header behavior remains intact. Explicit retries caused by a
confirmed-not-sent ConnectError remain allowed for declared read-only work and
still respect three-attempt and cumulative-wait limits. Operation/sleep
interruptions retain UNKNOWN and propagate instead of being swallowed.

The legacy evidence log has 22 discovered cases because its original fixture
import also exposed 15 existing TestCase methods; only the seven new methods
were the intended batch. The import was changed to a module import before the
fixed run to avoid duplicating those unrelated methods. Five failing assertions
in four methods are preserved, not relabelled flaky. The later positive
explicit-retry case increases this batch to eight actual protocol tests.

## Source-to-model mapping

| Model action/fact | Actual owner / observation |
| --- | --- |
| Claim / Observe mismatch or inprogress | `ToolExecutor.execute` lock, fingerprint comparison and pending receipt |
| Invoke, calls, effects | nested `invoke`; calls counts actual function entries, effects is an independent environmental fact, not inferred from receipt status |
| Fail / LibraryRetry | Tenacity explicit TryAgain versus ordinary read-only retry predicate, and unchanged `classify_failure` |
| Assess / OwnedRetry | nested `stop` independently rechecks read-only and retryable class, then attempt, planned-wait and elapsed-deadline limits |
| WaitDelay / hint | nested `wait`, public RemoteModelError hint and existing HTTP Retry-After lower bound |
| Wake / Clock / Expire | actual sleep may return or be interrupted; monotonic deadline is checked again before function entry; refusal does not increment reported actual attempts |
| Finish / Interrupt / terminal | EMPTY/COMPLETE, late ERROR or interrupted UNKNOWN; final locked deep-copy receipt publication |
| Observe terminal | returns isolated receipt snapshot, never invokes again |
| Caller identity | `KnowledgePlanning.invoke_tool` uses session + turn request + tool identity; caller owns namespace and fixes source policy at turn entry |
| Public error preservation | `knowledge_platform_contract._call` and `knowledge_contracts.domain_call` |

The lock/publication sections are atomic abstractions; arbitrary Python
interruption between their statements is not proved. The caller namespace is
not replaced by global argument caching and no same-turn policy-change premise
is assumed. A request identifier is **not** backend idempotency or durable
restart protection. No external model, embedding or judge requests occur.

## Finite bounds and checks

One executor/request, two fingerprint classes, one secondary observation,
three actual attempts, zero or one effect per attempt. Abstract quarter-second
units: planned wait cap 4, elapsed deadline 8, hints 0/2/8, exponential backoff
1/2. Clock can advance independently, including while work is running; Wake
adds the planned sleep, allowing conservative oversleep schedules. This is a
bounded safety exploration, not hard real-time deadline enforcement or a claim
that arbitrary function/sleep code terminates. No liveness/fairness property is
asserted. Real .25/.5/.6/100-second values are exercised with injected sleep,
not wall-clock races or paid providers.

Positive invariants: type domains, no declared mutable replay, no UNKNOWN
replay, admitted wait respects hint, terminal reuse makes no actual call,
receipt attempt snapshot matches actual entries, and attempt/wait bounds.
Three negative profiles reproduce the old explicit retry/preserved-public-hint
faults. `SeededTerminalReplay` is deliberately invented, **not** a reported
current defect. It reopens a terminal receipt and must reach an actual extra
invocation. Every negative run must fail its named invariant with a complete
TLC counterexample, not a parse/error exit.

`tool_retry_lint.py` is called by `registry_violations`, hence the ordinary
mandatory `formal-lint` gate and formal-only entry, not merely an optional test.
Discovered formal contracts reject 15 seeded source mutations and specifically
refresh all owner hashes after restoring the legacy stop, proving hashes alone
cannot satisfy the guard. This is focused semantic structure checking, not a
compiler-level source refinement proof. Remaining obligations include multiple
executors/restarts, arbitrary argument/deepcopy failures, clock precision,
unbounded scheduling and remote/auth/format owners.

Initial model diagnostic `/tmp/evidence-tool-models-20261102T205900Z` retained
exit 75 for two controls: boolean primed assignment RHS parentheses were
missing and produced an unspecified successor variable. Those runs were FAIL,
not accepted negative controls. Corrected source is checked in later runs.

## Saved candidate validation

- Final targeted contracts: **37 tests, 0 failures/errors/skips**, exit 0.
- Final TLC: positive **31,100 generated / 16,308 distinct / queue exhausted**;
  mutation and UNKNOWN legacy controls each 2,928/1,808, public-hint control
  906/615, actual terminal-replay seed 425/321. All four named controls exit 12.
- Architecture and complete formal registry lint: no violations. AST comparison
  with the exact base confirms every provider_runtime owner outside
  `ToolExecutor.execute` is unchanged. Ruff and diff whitespace checks pass.
- Required ordinary and formal-only entrypoints will run on the frozen commit;
  this saved candidate does not claim their acceptance before those runs finish.
- `evidence/SHA256SUMS` binds reviewed source/model inputs and retained execution
  artifacts. Evidence is offline component/model verification, not RAG quality.

## PR54 Windows fixture cleanup follow-up

The first exact-head CI run `41533447595` remains **FAIL**. Both Windows
Python 3.12 and 3.13 quality artifacts contain two `WinError 32` errors deleting
`effects.sqlite3` during TemporaryDirectory exit; all product retry assertions
had completed. A sqlite3 Connection context manager controls transaction
commit/rollback but does not close the connection. POSIX unlink had hidden this
test-fixture ownership error. It is not a regression in the product retry fix,
and is unrelated to the PR51 Tantivy commit failure.

All three fixture connections now use `closing(connection)` outside their
unchanged transaction context. The write commits before TryAgain is raised,
then the connection closes before directory removal. There is no cleanup
suppression, GC retry, sleep, relaxed assertion or production/TLA change.
A real connection observer retains all three references and checks that each
rejects SQL before TemporaryDirectory cleanup; the old fixture fails that
check, while the fixed fixture passes. Its safety-finally closes references
even when this deliberately challenged assertion fails.

Mandatory formal-lint now checks the three close/transaction contexts and
operation exception boundary, with a source binding for the real fixture.
Restoring the old connection context and refreshing all registry hashes still
fails this guard. Evidence in `evidence/fixture-cleanup/` retains the original
Windows diagnoses, old behavioral failure, an initial mistyped test-module
invocation, and the corrected 38-test PASS. The earlier evidence SHA256SUMS
continues to describe the earlier frozen candidate, not these changed sources.
