# Knowledge workflow: graph completion is not business success

This is a finite, safety-only model of one serialized knowledge turn. It is a
partial abstraction, not a proof of arbitrary generated answers, the generic
graph implementation, or the session database.

## Source map

| Model action or field | Runtime owner |
| --- | --- |
| SourcePolicy, ValidateSources, epoch, derived, workEpoch | KnowledgeTurn.run source-policy/authority invalidation, evidence revalidation, and begin_work_state |
| Plan, PlanExplorationStop, ToolSuccess, ToolFailure, RepeatedTool | KnowledgePlanning.plan_node / tool_node |
| ToolCorrection, inputCorrections, correctionSafe | KnowledgePlanning.tool_node allows one recognized local input correction for a retry-safe, read-only operation with no cost, within the existing step and answer budgets |
| Compose, Verify, RepairSuccess, RepairFailure, Deliver | KnowledgeAnswer nodes and KnowledgeWorkflow._choose_route |
| BeginNode, AfterFailure, AcceptNode | KnowledgeTurn.before_node, Runtime.run, GraphProgram.execute / accept, Rust Graph::accept |
| graphDone, RuntimeCompletionPass / Failure, post_runtime | graphDone is native route terminality after deliver acceptance; GraphProgram.next, kernel finish, and program close must still succeed before Runtime returns; exceptions go to failure |
| PostRuntimeFailure | KnowledgeTurn.run checks execution and rejects a returned response with status == "error" before success persistence |
| AuthorityPass, RevalidatePass, FinalCheckPass, Finish, Fail | KnowledgeTurn.run final authority/evidence/execution checks and existing store.finish / redacted store.fail; knowledge_state.validate_failure |

The important path is not an exception inside the graph. The tool node catches
an ordinary tool failure, preserves its receipt, and returns a normal halted node
result. Compose then produces a nonempty explanatory error answer; deliver can
still carry earlier evidence lineage. The graph therefore reaches complete.
The new business-error guard must divert that result to the existing failure
boundary before last_answer, an assistant event, or a successful finish is
written. The model deliberately retains the nonempty intermediate error
payload; it does not assume redaction happened inside compose or generic graph
completion.

The product integration adds `ToolCorrection` for the bounded input-correction
path. `eligible` abstracts the conjunction of the runtime predicates above,
including remaining budget and the non-fast-calculation path. Existing
`AgentGraphContracts` execute those predicates. The positive model checks
`SingleInputCorrection` and `NoUnsafeInputCorrection` in addition to every
previous invariant. Three controls reach an eligible correction, reject an
unsafe correction with its guard removed, and reject a second correction with
the guard removed at `MaxSteps=3`. The positive bound remains `MaxSteps=2`.

## Source-policy timing and repeated-tool boundary

`SourcePolicy` and `ValidateSources` represent turn initialization, before the
first `BeginNode`/`Plan`, not a planner-selectable mid-turn source switch. In the
mapped implementation, `KnowledgeTurn.run` applies the validated request policy,
invalidates prior evidence/catalog/offsets/checkpoints, then creates a fresh
`seen_actions` set before graph execution. The current built-in tool registry
and generated decision schema have no `select_sources` action. A policy change
is an explicit request control on the next turn; the unchanged query can then
execute against the newly selected sources.

A read-only audit of the fixed implementation checked the actual schema rejection
and executed two ordinary turns through `KnowledgeService`, Python/native Rust
graph execution and SQLite sessions. The same `search(query="original")` ran
once for locked source `s0` and once for locked source `s1`; only `s1` evidence
was delivered by the second turn. The action/argument digest was identical, but
the actual tool receipt keys differed by request id. Replaying the second request
returned the same JSON response without another model or tool execution. Model
completions and authority/search inputs were synthetic, not paid providers or
production HTTP/PostgreSQL evidence.

Existing `AgentGraphContracts` cases already require same-turn repeated arguments
to halt without reexecution, remove old source catalog entries after a new-turn
policy change, and reject a cross-turn stale quote. The audit found no supported
`search → select_sources → search` path and does not introduce a product fix or
claim a new bug. Receipt-key correctness, arbitrary plugin effects, and external
authority changes are not proved by this model; its `RepeatedTool` action
abstracts an already detected repetition rather than modeling digest/cache keys.

## Bounds and transitions

- MaxSteps = 2: zero, one, or two ordinary tool actions, including a successful
  action before a later failure. Repeated-action detection does not add a physical
  tool receipt. Tool receipts already obtained are never rolled back.
- All six graph nodes are separate actions. Dispatch, node execution, and
  acceptance are separate transitions, with execution checks before dispatch
  and after execution. An external stop can arrive after an effect but before
  acceptance.
- Semantic graph repair is enabled or disabled nondeterministically. The state
  space permits **two** repair entries so removing the one-repair guard is
  detectable; the positive invariant permits at most one. A repaired answer is
  always verified again. Earlier schema/format/exact-citation repair inside
  call_answer, and abstention review, are separate mechanisms and are not
  included in this counter.
- Source epochs are 0..2, with numeric tag 3 meaning absent. Policy or authority
  invalidation can advance the epoch once; failed ordinary evidence revalidation
  can advance it again. Catalog, offsets, checkpoints, and prior answer are
  cleared. Policy change never carries evidence. Auth-only change may carry
  pinned evidence **only as revalidated current-epoch evidence**; freshness tags
  do not mean the pinned document version changed.
- ValidateSources includes binding newly active work_state to the final epoch.
  Workflow-state user fields, history truncation, actual source-policy matching,
  and pinned-version authorization internals are not modeled.
- Candidate text/claims/evidence and derived trace are presence bits; verifier
  verdicts are nondeterministic validated inputs, not truth or semantic oracles.
  Explicit completion is enabled in this abstraction. Answered, partial,
  conflict-as-partial, abstained, and clarification may all finish normally.
  Unsupported verification cannot publish answered claims.
- Out-of-domain completion, general/greeting/calculation fast paths,
  user-statements-only verification, and answer_contract=False abstention
  combinations are not represented. Repaired answered candidates use complete
  before the next verifier outcome; arbitrary repaired completion payloads are
  not exhaustively represented. These omitted branches are not proved by this
  finite configuration.
- One sticky external signal represents cancellation, exhausted execution
  budget/deadline, or changed authority. Exploration budget is separately a
  normal partial-answer route, not an execution-budget failure. Provider calls,
  physical reservations, retries, and session/kernel internals remain owned by
  their existing models.

## Invariants and negative controls

The positive configuration checks:

- TypeOK, TerminalCommitConsistent
- FailedBusinessUsesRedactedFail, NoFailedAnswerPromotion
- SingleSemanticRepair
- SourceEpochClearsDerivedContext, CurrentWorkEpoch
- UnsupportedDoesNotAnswer, ExplorationBound
- ReceiptsSurviveFailure, ObservedStopCannotFinish

Each negative checks its named invariant with exactly one disabled protection:

| Configuration | Changed constant | Expected violation |
| --- | --- | --- |
| LegacyBusinessErrorCommitNegative.cfg | FailBusinessErrors = FALSE | FailedBusinessUsesRedactedFail: a tool error reaches graph completion and finish with a nonempty answer |
| UnboundedSemanticRepairNegative.cfg | BoundSemanticRepair = FALSE | SingleSemanticRepair: unsupported verification enters repair twice |
| StaleSourceCheckpointNegative.cfg | ClearSourceContext = FALSE | SourceEpochClearsDerivedContext: a policy/authority reset retains an old checkpoint |

Only the first negative reproduces the business-error publication defect. The
other two are seeded missing-guard controls for existing behaviors, not claims
that those defects were observed in production.

## Interface assumptions and limits

The generic graph is assumed to accept exactly the operation just executed;
this model does not duplicate its effect-id matching or lifecycle proof. An
ordinary uncaught node error ends the turn without automatic graph replay.
Each store call is abstracted as one atomic durable result; SQL transaction
isolation, writer-token fencing, crash recovery, and failed commit acknowledgments
are separate obligations.

Checks are **sampled checkpoints, not a global atomic fence**. The model permits
an authority change after the final authority check, and a cancellation after
the final execution check, to interleave before Finish. It asserts only that a
failure actually detected at a modeled check cannot take the finish path. It
does not establish authorization/cancellation atomicity with the database or
claim that completed physical tool effects disappear on interruption.
Revalidation is an abstract success/failure input; its implementation is not
proved here. Receipts survive failure, but their accounting accuracy is outside
this model.

No fairness is imposed, no liveness property is claimed, and deadlock checking
is disabled for intentionally terminal states. Reachable successful, partial,
abstained, clarified, and failed completions are checked separately as
non-vacuity evidence; reachability is not eventual completion.

## Checked evidence (2026-11-02)

Direct TLC runs used Java 21 and the pinned jar with SHA-256
936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88.
The positive exhausted the finite graph with all eleven invariants intact.

| Configuration | Exit | Generated / distinct | Result |
| --- | ---: | ---: | --- |
| KnowledgeWorkflow | 0 | 90,230 / 50,592 | No violation; 0 queued; depth 37 |
| LegacyBusinessErrorCommitNegative | 12 | 43,699 / 25,402 | FailedBusinessUsesRedactedFail; depth 21 |
| UnboundedSemanticRepairNegative | 12 | 33,757 / 18,531 | SingleSemanticRepair; depth 19 |
| StaleSourceCheckpointNegative | 12 | 4 / 4 | SourceEpochClearsDerivedContext; depth 2 |

The positive state dump contains 156 answered, 1,172 partial, 640 abstained,
80 clarification, and 5,282 error terminal states. There are 520 normally
finished turns after one semantic repair, 48 graph-terminal/pre-redaction error
states carrying evidence, and 4,778 failure terminals retaining internal
lineage while the response evidence is empty. Epoch 2 is reachable.
The dump also contains 1,536 finished states with a late external signal,
deliberately exposing the sampled-checkpoint limitation rather than silently
assuming an atomic authorization/cancellation fence.

These are model-state counts, not requests, user sessions, production rates, or
semantic accuracy scores. Logs, the exact model/config snapshot, the reachable
state dump, and a checked JSON summary were saved under
/tmp/knowledge-workflow-tlc-final-20261102-0IQt8f. The parent integration separately
owns the ordinary checker, source binding/lint, and formal-only gate.

## Mature reference boundaries

The repository's [pinned upstream comparison](../../docs/upstream-recovery-and-task-scope.md)
records the adopted boundaries. In particular, the pinned
[DSH agent documentation](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent/README.md)
distinguishes committed assistant messages from failed attempts; the pinned
[pi agent loop](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/agent/src/agent-loop.ts)
ends the turn on assistant error/abort. The repository also records Codex's
interruption-not-rollback boundary. These are interface precedents, not a claim
that those projects implement this knowledge graph or its semantic verifier.

## Reproduction

Use Java 21 and the repository-pinned TLA+ tools 1.7.4 jar (TLC reports 2.19):

~~~sh
cd formal/knowledge_workflow
OUT="$(mktemp -d /tmp/knowledge-workflow-tlc-XXXXXX)"
java -cp /tmp/evidence-tla-tools/tla2tools-1.7.4.jar tlc2.TLC \
  -workers 1 -config KnowledgeWorkflow.cfg -metadir "$OUT/states" \
  -dump "$OUT/reachable-states.dump" KnowledgeWorkflow >"$OUT/positive.log" 2>&1
~~~

Run each negative with its own fresh metadata/log directory. A negative is
successful evidence only when TLC exits for the specified invariant violation,
not for parse, type, or execution errors. Source hashes in the registry are
review-drift detection, not a refinement proof between Python/Rust and TLA+.
