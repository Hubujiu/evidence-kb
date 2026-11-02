# Plugin lifecycle: superseded initialization

## Defect and mature implementation reference

A provider replacement drains its consumers. If a consumer's already-claimed
initialization then fails, main `8160c051e63952278c5f8a80d1b024ebbe3cea17`
unconditionally sets `failed_init`. Unlike a direct consumer update, a dependency
update does not install the consumer's `next_manifest`; final cleanup therefore
leaves that consumer `Failed` after its new provider becomes active.

The behavioral reference is the repository's pinned [DeepSeek Harness Cordis
fiber implementation](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/fiber.ts):
`_reload` (lines 637–665) only records an initialization error when its activation
epoch is still current; `_unload` (667–700) retains providers until dependent
cleanup finishes. This is vendored Cordis 4.0.0-rc.11 with DSH changes, not a claim
about every Cordis release. The existing source lock pins `fiber.ts` SHA-256
`b64c3a97f15f0381479dcf3d25965e56dc4cecfa4a0881fc055a49f65b9945df`.
The source was inspected before making this fix; no upstream application or model
API was executed.

The correction records an init error as current only if the instance was still
`Starting` before completion. Drained old work still owns and cleans its resources.
A genuine current error remains terminal, as does explicit cancellation, whose
failure cause is already recorded by `cancel`.

## Model and implementation mapping

This is a bounded behavioral abstraction, not a machine-checked refinement proof
from Rust to TLA+. Deterministic Rust tests connect the counterexample to the
actual coordinator. Source-binding lint detects drift requiring review; hashes do
not prove semantic equivalence.

| TLA+ state/action | Rust implementation |
| --- | --- |
| `phase`, `generation`, `desired`, `failure` | `Instance` and `Phase`/`Desired` |
| `initializing` | claimed `Instance.init` operation, before real completion |
| `binding` | consumer's retained provider generation in `Instance.bindings` |
| `resources` | number of unfinished resources; resource identity/order abstracted |
| `calls` | one invocation lease per owner; `cancelled` remains live |
| `nextManifest` | pending replacement manifest |
| `UpdateProvider` | `update` → `drain_instance` → invalid-dependency `reconcile` |
| `CompleteInit` | `complete` init arm; legacy/fixed error ownership toggle |
| `StartInit`, `BeginCleanup` | `reconcile` initialization and cleanup eligibility |
| `CleanupResource`, `FinishCleanup`, `CleanupError` | `complete` cleanup arm |
| `RetryCleanup` | `retry` |
| `Invoke`, `CancelCall`, `CompleteCall` | `invoke`/`cancel`/`complete` and lease removal |
| `RemoveScope` | `drain` and reconciliation |

Production reconciliation runs synchronously after commands. The model splits its
internal progress into actions, but provider replacement atomically invalidates
both active/starting nodes, so it does not invent a visible active consumer using
a drained provider. Cleanup waits for the consumer binding and real call/init
completion. Cleanup errors retain resources and bindings: the project's stricter
policy intentionally differs from upstream's log-and-continue cleanup.

## Bounds, assumptions, and omissions

- One scope, two plugins, one provider-consumer edge; provider starts active,
  consumer starts with one claimed init. Each initially owns one resource.
- At most two generations, two unfinished resources and one invocation per plugin;
  one provider replacement, at most one cleanup failure per plugin, optional removal.
- The first consumer init may succeed or fail at either side of replacement. Later
  init work succeeds. Current failures are not automatically retried.
- Weak fairness for real init/call completion, enabled reconciliation, cleanup and
  explicitly requested cleanup retry is required for the two liveness properties.
  Cooperative completion is an environment assumption; cancellation cannot force
  arbitrary blocking foreign code to finish.
- No fairness forces provider update, resource creation, or removal to occur.
- This model does not cover all coordinator behaviors: nested scopes, serial-call
  ancestry, multiple dependencies, disabled/re-enabled scopes, unclaimed actions,
  operation acknowledgments, listener ordering and process transport are excluded.
  Neither the resource count nor source hashes prove LIFO resource identities.
- `CHECK_DEADLOCK FALSE` permits intended stable terminal states; progress is checked
  by the explicit temporal properties, not inferred from deadlock suppression.

## Checks and retained evidence

`PluginLifecycle.cfg` checks seven invariants and two temporal properties.
`PluginLifecycleNegative.cfg` changes only `PreserveSupersededFailure` to `TRUE` and
must exit 12 with `NoSupersededFailure` violated, not a parse/runtime failure.
Its three-state trace is: active provider / starting consumer → provider update →
consumer's superseded error. The same old implementation failed the Rust regression
because no consumer restart action was produced after provider recovery.

Retained logs in `evidence/` are from this task's executions, not historical PASS
substitutes. TLC 2.19 / tla2tools 1.7.4 JAR SHA-256:
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
The initial positive check completed with 10,307 generated / 4,821 distinct states,
depth 27, including both temporal properties. The Rust baseline regression failed
before the source change (exit 101), and the entire plugin-runtime suite is rerun
after the change. See the checked logs for actual outcomes; this is not a full
project or operating-system acceptance claim.

Run from this directory with a verified local JAR:

```sh
java -Xmx256m -XX:+UseParallelGC -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -workers 1 -config PluginLifecycle.cfg -metadir /tmp/plugin-lifecycle-new PluginLifecycle.tla
```

Run the Rust regressions from the repository root:

```sh
cargo test --locked --offline -p plugin-runtime
```
