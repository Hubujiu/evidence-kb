# Provider circuit: generation-fenced completion

## Defect and mature precedent

At base `8160c051e63952278c5f8a80d1b024ebbe3cea17`, circuit admission returned only
a Boolean indicating a probe. A previously admitted normal request could finish
after a newer circuit transition. Its success unconditionally cleared
`_opened_at`, including while a half-open probe was still running. The snapshot
then said `HALF_OPEN`, but admission took the closed-circuit branch and permitted
another request. Old failures could also extend a newer cooldown or contaminate
new closed-generation failure counts.

The inspected mature precedent is Sony's
[`gobreaker.go`](https://github.com/sony/gobreaker/blob/fed8e9eb35f9cd3e5c2a67842c924346c3e1fbdd/gobreaker.go):
`beforeRequest` returns a generation, `afterRequest` ignores a mismatching
generation, and `setState` calls `toNewGeneration`. Its
[`TestGeneration`](https://github.com/sony/gobreaker/blob/fed8e9eb35f9cd3e5c2a67842c924346c3e1fbdd/gobreaker_test.go)
tests that prior-generation completions do not change current counts. This is a
design reference, not copied or translated implementation. The inspected source
is [MIT licensed, copyright 2015 Sony Corporation](https://github.com/sony/gobreaker/blob/fed8e9eb35f9cd3e5c2a67842c924346c3e1fbdd/LICENSE).
Other circuit libraries need not use this exact policy. This change does not
claim that DSH implements persistent budgets, receipts, or circuit generations.

## Internal API and preserved behavior

`ProviderCircuit.acquire()` now returns an opaque identity permit carrying its
generation. `finish(permit, ...)` consumes it at most once. The issuing circuit's
active set rejects foreign, consumed, and invalidated permits. `_probe` owns the
specific current permit, rather than a Boolean flag. Generations advance when
opening, admitting a half-open probe, or completing that probe. These transitions
invalidate old active permits; already-running provider calls are not cancelled.

The only production caller is `remote._provider_guard`; it passes the permit
through without interpreting it. There are no circuit consumers in
`provider_admission.py`, so its durable-admission API is unchanged. This is an
intentional internal API replacement, not a Boolean compatibility layer.

- A valid current-generation success closes the circuit and resets failures.
- A valid threshold-crossing normal failure or probe failure opens the circuit
  and starts a new cooldown.
- A neutral probe completion releases its own slot but stays `OPEN`, preserving
  the original opening time and failure count. Since its original cooldown has
  already elapsed, another probe can be admitted immediately, as before.
- Stale/duplicate completion does not alter current state, counters, or cooldown.
  The caller still receives its original result or exception.
- Failure classification, retry decisions, threshold configuration, snapshot
  shape, and provider error codes are unchanged.

An admitted caller must eventually call `finish`, including on exceptions;
`_provider_guard` handles `BaseException`. Abandoned normal permits remain active
until a circuit generation changes. No watchdog or provider cancellation is
added by this change.

## Finite model and counterexample

[`ProviderCircuit.tla`](../formal/provider_circuit/ProviderCircuit.tla) includes
the original unowned-completion behavior under `Legacy = TRUE`, and the fenced
design otherwise. The model's immutable `permits[r].probing` records whether an
admission owns a probe; implementation ownership is token identity. The model's
`completed` set and generation comparison correspond to consumption and
invalidation in `_active`. Method bodies are atomic under the Python lock.

The actual legacy TLC run exited **12**, emitted framed event **2110** naming
`ProbeRequiresOpen`, and produced this seven-state counterexample:

| State | Transition | Open | Probe |
| --- | --- | --- | --- |
| 1 | Initial | false | none |
| 2 | Admit a normally | false | none |
| 3 | Admit b normally | false | none |
| 4 | a fails at threshold 1 | true | none |
| 5 | Cooldown elapses | true | none |
| 6 | Admit c as probe | true | c |
| 7 | Old b succeeds | **false** | **c** |

This run generated 240 states and found 102 distinct states before stopping at
the intended violation. An earlier model-construction error was corrected and
is not counted as a negative control. The legacy config disables duplicate
finishes so this counterexample does not depend on caller misuse.

Positive configurations check `TypeOK`, `ProbeRequiresOpen`,
`ProbeHasCurrentOwner`, `ProbeExcludesAdmission`, `NoStaleMutation`, and
`NoDuplicateMutation`. `liveness.cfg` and `threshold_two.cfg` also check
`ProbeEventuallyReleased`, under weak fairness of each admitted request's
completion. Duplicate finishes remain enabled in all positive configurations.

| Configuration | Bound | Actual result |
| --- | --- | --- |
| `safety.cfg` | 3 admissions, threshold 1 | exit 0; 1,745 generated / 173 distinct; depth 9 |
| `liveness.cfg` | 3 admissions, threshold 1, fair completion | exit 0; 1,745 / 173; depth 9 |
| `threshold_two.cfg` | 5 admissions, threshold 2, fair completion | exit 0; 50,307 / 3,046; depth 13 |

The five-admission bound includes an old call, two threshold-crossing failures,
a recovery probe, and a new closed-generation failure. Each request ID is
admitted at most once. Counters saturate at the threshold because larger values
do not change modeled transition decisions; exact snapshot counts are tested in
Python instead. Time is a Boolean cooldown-crossing abstraction. No state
constraint or symmetry reduction removes behaviors inside these bounds.

These are **partial bounded checks**, not an unbounded proof or a proved
refinement of Python. They exclude process restart, persistence, provider
execution, distributed coordination, fairness of the clock or scheduler,
ToolDispatcher retry behavior, and malformed direct use of the internal API.
Probe-release liveness assumes completion; a permanently hung provider can keep
the circuit half-open indefinitely. Recovery to `CLOSED` additionally needs a
successful probe and is not claimed.

## Reproduction and offline contracts

The executed checker is official
[TLA+ tools v1.7.4](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4), source
[`5a47802b5c391f59ecdd44117981f4ff8c0656ba`](https://github.com/tlaplus/tlaplus/tree/5a47802b5c391f59ecdd44117981f4ff8c0656ba),
under its [MIT license](https://github.com/tlaplus/tlaplus/blob/5a47802b5c391f59ecdd44117981f4ff8c0656ba/LICENSE).
The `tla2tools.jar` SHA-256 verified before execution is
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
TLC identifies itself as version 2.19, revision `5a47802`. Runs used OpenJDK
21.0.12.1, one worker, and a 512 MiB maximum heap. No jar is vendored here.

From `formal/provider_circuit`, with a verified jar path and a fresh metadir:

```sh
java -XX:+UseParallelGC -Xmx512m -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -tool -workers 1 -metadir "$NEW_METADIR" -config safety.cfg ProviderCircuit.tla
```

Repeat with the other configs and distinct metadirs. Positive runs require exit
0, TLC success event 2193, completed graph statistics with no queued states, and
completed temporal checking for property-bearing configs. The legacy run must
fail with the named invariant and exit 12, not merely any nonzero exit.

`kb_platform.rag_testing.provider_circuit_contracts` contains 12 synthetic,
offline unittest contracts. Against the original source it ran 12 tests and
reported 11 failures, including subtest failures. Against the fix all 12 pass.
Coverage includes stale successes/failures during open, half-open and recovered
closed generations; duplicate and foreign permits; eight-way concurrent probe
admission; neutral timeout; cooldown timing; and a real `_provider_guard` call
interleaving coordinated by events. No remote provider is called.

Targeted Ruff and `git diff --check` pass. The required unified checker was
attempted but uv stopped before executing it because its default cache directory
was read-only. The parent integration run owns full-project gate execution;
this document does not claim a full-project or CI pass. New contracts are
automatically discovered by the existing unified framework. Newly incurred
model, embedding, and judge calls: **0**.

## Per-fix design lint

`scripts/formal_provider_lint.py` parses the actual Python AST. It requires the
completion method's sole outer block to hold the circuit lock; the first branch
must immediately return on an inactive **or** stale permit. The next statement
must consume that permit, before outcome effects, and the next must derive probe
ownership by identity. Probe release and generation advance must occur under the
ownership conditional. The rule also limits direct lifecycle-field writes and
active-permit mutations to their designated methods and requires frozen,
identity-based permit equality. It compares AST structure, not text substrings
or the mere presence of a generation field.

Four additional unified contracts lint the production file, accept equivalent
formatting/decorator-keyword order and unrelated edits, reject missing/invalid
source, and reject twelve seeded weakening mutations. All 16 circuit and lint
tests pass. The formal checker integration is an explicit known hook, not a
dynamic registry-specified code loader; its small integration patch is supplied
separately for the parent to apply on the formal-infrastructure base.

This is a deliberately narrow design constraint, not general Python effect
analysis or a semantic/refinement proof. Alternative equivalent control-flow
shapes require deliberate rule review. AST source hashes remain independent
review-drift checks; refreshing a hash must not disable this structural rule.
