# Agent execution: three bounded safety models

This stage adds formal coverage and concrete regression tests, not a production
behavior fix. Inspection and the stated checks did **not** confirm a defect in
`agent-kernel` or the built-in `agent-services` strategies. The negative controls
are deliberately broken models, not claims that these bugs existed in main.
Baseline: `8160c051e63952278c5f8a80d1b024ebbe3cea17`.

## Mature implementation references, inspected before modeling

- [Codex `f752c97`, interrupted-turn context](https://github.com/openai/codex/blob/f752c97f7d0be0cbef5cece327f52a16ad6fb1c6/codex-rs/core/src/context/turn_aborted.rs),
  Git blob `f0bd9580607856b51c1fa4f8053ccab3febf028d`: interruption may leave
  commands partly executed or background processes running. Consequently our
  terminal/closed state means no further logical admission, **not** rollback,
  actual process termination, zero spending or resolved UNKNOWN effects.
- [Pi `4b535d5`, agent loop](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/agent/src/agent-loop.ts#L251),
  Git blob `cfc9401e5581847f6d5a9bb1815ab0f0e663f2e5`: failed/aborted assistant
  attempts stop before tool execution; tool results are added before continuation.
  The local model checks our stricter protocol-level call/result append order,
  not Pi API equivalence or every upstream continuation feature.
- [DSH `ce7073d`, public agent contract](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent/README.md):
  the driver is replaceable and cancellation is distinct from quiescence;
  process-local observations are not the durable replay source. Our loop/graph
  handles remain transient, and persistence and physical cancellation stay with
  their existing owners.

These fixed sources are also recorded in
`docs/upstream-recovery-and-task-scope.md`. Codex/Pi were read through GitHub's
file connector after web cache misses; DSH was read from its fixed primary page.
No upstream application, paid model, embedding, or judge was executed.

## Source-to-model mapping

These are manually reviewed behavioral abstractions. Neither source hashes nor
matching reachable-state counts constitute a machine-checked refinement proof.
All `Apply`, `Issue` and `Accept` transitions are atomic at the Rust command
boundary; the caller serializes a kernel state, and the service registries are
mutex-protected. There are no modeled concurrent accesses within one command.

| Model state/action | Concrete source and behavior |
| --- | --- |
| Kernel `state.phase/count/pendingId/pendingKind/now` | `agent-kernel/src/lib.rs`: `State` fields `phase`, `operations_started`, `pending_id`, `pending_kind`, `last_now_ms`; maximum/deadline fixed by model bounds |
| Kernel `Apply`, `ShapeOK`, `Stop` | `transition`, `event_shape`, `stop`; guard order is preserved, including shape/correlation rejection before deadline expiry |
| Kernel `TerminalAbsorbing`, `SettlementCorrelated`, `DeadlineEnforced` | pre/post monitors check terminal immutability, kind+ID settlement, successful events before deadline |
| Kernel `PendingMatches`, `ExpiredClock`, `LimitReached` | reachable-state portion of `valid_state`; corrupt input snapshots are separately tested in Rust |
| Loop `s.phase`, `pending`, `sequence`, `tools` | `agent-services/src/agent_loop.rs`: `ServiceLoop::phase/pending/sequence/tool_count`; the three `Append` continuations are separated as model phases |
| Loop `Issue`, `Accept`, `Advance` | built-in `ServiceLoop::next`, `accept`, `accept_phase`; registry `next/accept` removes a handle on a program error |
| Loop `callRecorded/resultRecorded/answerRecorded` | ghost facts that the matching `session.append` effect was acknowledged, not proof that a backend durably committed it |
| Loop `cached`, `mode`, `direct` | `session.start` cached-result shortcut (including null), trusted single-tool selection, manifest `direct_return` |
| Graph `node/pending/sequence` | `agent-services/src/graph.rs`: `Graph::current/pending/sequence`; complete represents `current=None` |
| Graph `Issue`, `Accept`, `Routes`, `Target` | `Graph::next/accept`; a two-node declared graph with a calculate self-loop and publish terminal edge |
| Both service models `CloseHandle` / `closed` | explicit close or registry error removal, represented by a tombstone; not asynchronous cancellation or effect rollback |

The Python `agent_native._State` and `_NativeProgram.accept` are existing ABI and
result-forwarding bindings. Their layout/forwarding is reviewed, not modeled as a
second state authority. `agent_engine.Runtime` checks external cancellation and
deadlines around effects; those multi-layer timings are outside these models.

`LoopProgram` is deliberately replaceable trusted policy. Correlation and phase
invariants apply to the two **built-in** `ServiceLoop` strategies. The registry
removes a program on its error; it does not impose the default strategy on an
arbitrary custom plugin. No new enforcement was added at that extension point.

## Bounds, fairness and excluded states

- Kernel: one valid run; maximum two operations; deadline 2; observed times
  0..3; three operation kinds plus none/invalid; event IDs 0..3; five event
  kinds plus invalid. There is no initial-state corruption in TLA+; Rust tests
  additionally reject malformed value snapshots and exercise u64 exhaustion.
- Loop: both modes and both direct-return values; one abstract available tool;
  at most two successful tool results, hence at most 14 issued effects. Selecting
  a third tool is excluded, not treated as a production operation limit. JSON
  values are collapsed into valid-result classes and an invalid class; append
  and tool acknowledgments each represent arbitrary payload values. Unknown
  tools/malformed decisions are represented by rejection, not concrete parsing.
- Graph: two nodes, one self-loop, three issued effects, IDs 0..4, two valid route
  labels plus an undeclared label. Issuing new work past the finite effect bound
  is excluded, not an invented graph limit. Full u64 exhaustion is tested in Rust.
- Safety only: `Spec` has **no fairness assumptions** and no liveness properties.
  A caller may stop sending events; a real graph may cycle forever; an external
  effect may never return. No eventual completion or deadline polling guarantee
  is claimed. Deadlock checking is disabled to permit intended stable/closed and
  finite-bound states; this is not evidence of progress.
- Not covered: multiple handles/registry identity exhaustion in TLA+, registry
  capacity, arbitrary user `LoopProgram`s, all graph topologies, unbounded tools
  and histories, JSON/FFI memory safety, Python scheduling/context propagation,
  actual tool/model effects, storage commit or UNKNOWN recovery, OS/process
  interruption, paid-provider behavior, or production RAG quality.
- Existing concrete tests additionally cover independent/nonreused handles,
  malformed graph configuration, unknown tools, cached nulls and protocol errors.
  Those tests do not expand the formal bounds.

## Executed checks and negative controls

TLC 2.19 / tla2tools 1.7.4 JAR SHA-256:
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
Actual logs are retained in `evidence/`; the JVM reports its local timezone in log
headers. They are this stage's evidence, not a substitute for a later gate run.

| Positive configuration | Generated / distinct states | Search depth |
| --- | ---: | ---: |
| `KernelExecution.cfg` | 20,641 / 43 | 6 |
| `LoopSequencing.cfg` | 7,785 / 130 | 30 |
| `GraphSequencing.cfg` | 241 / 18 | 7 |

Nine negative configurations each actually exited **12 at the named invariant**:

| Deliberately broken guard/order | Expected failed invariant |
| --- | --- |
| `KernelCorrelationNegative` | `SettlementCorrelated` |
| `KernelTerminalNegative` | `TerminalAbsorbing` |
| `KernelDeadlineNegative` | `DeadlineEnforced` |
| `LoopCorrelationNegative` | `SettlementCorrelated` |
| `LoopCallRecordNegative` | `ToolRequiresCallRecord` |
| `LoopResultRecordNegative` | `CompleteRequiresRecord` |
| `GraphCorrelationNegative` | `SettlementCorrelated` |
| `GraphRouteNegative` | `DeclaredRouteOnly` |
| `GraphPendingNegative` | `IssuedEffectNotReplayed` |

The first loop draft mistakenly bounded effects at 13. TLC found the legitimate
14th effect (final answer append after two tools), violating that draft's
`TypeOK`. `loop-bound-discovery.log` is retained to distinguish a model-bound
correction from a product defect. The corrected bound was fully rechecked.

The real Rust kernel exploration independently visits all 43 reachable concrete
states and exercises 24,080 input transitions (it includes two invalid numeric
event tags collapsed into one TLA+ class). Its invariants do not reproduce the
transition algorithm. Targeted tests pass: 14 `agent-kernel`, 21 `agent-services`;
seven are newly added. Format and targeted Clippy (`-D warnings`) were run. The
parent integration owns the required full project checker and CI; these targeted
results do not stand in for that gate.

Run any positive model from repository root, using a verified JAR:

```sh
java -Xmx256m -XX:+UseParallelGC -cp "$TLA2TOOLS_JAR" tlc2.TLC \
  -workers 1 -config formal/agent_execution/KernelExecution.cfg \
  -metadir /tmp/agent-execution-new formal/agent_execution/KernelExecution.tla
cargo test --locked --offline -p agent-kernel -p agent-services
cargo clippy --locked --offline -p agent-kernel -p agent-services --all-targets -- -D warnings
```

`formal/registry-v1.json` is the sole active registry. The reviewed handoff was
merged into the existing `kernel_execution` and `loop_graph` inventory entries,
preserving their source bindings and invariants; the duplicate fragment was removed.
Both entries remain partial with explicit remaining obligations. The unified
`--formal-only` gate checks all three positive models and nine named negative controls.

Historical Rust test logs remain byte-for-byte in the published evidence snapshot:
[initial Rust test run](https://github.com/Hubujiu/evidence-kb/blob/562c72b859c2fb95aafc413b2d4af91d66382777/formal/agent_execution/evidence/rust-tests.log)
and [strengthened Rust test run](https://github.com/Hubujiu/evidence-kb/blob/562c72b859c2fb95aafc413b2d4af91d66382777/formal/agent_execution/evidence/rust-tests-strengthened.log).
These immutable links preserve the original raw logs, including their trailing
blank lines, without duplicating or rewriting them in this candidate.
Current integration validation reruns the Rust tests and formal gate independently;
historical logs do not establish CI success for the final PR head.
