# Knowledge workflow completion is not answer success

## Mature precedents and scope

This change follows the already pinned upstream review, without copying code or adding dependencies:

- [DSH agent contract at ce7073d](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent/README.md) distinguishes completed assistant messages from failed assistant attempts and separates admission from committed history. A terminal notification is not proof of a successful business result.
- [Pi agent loop at 4b535d5](https://github.com/earendil-works/pi/blob/4b535d57ea8863dbba44b89872146523da271786/packages/agent/src/agent-loop.ts) ends failed or aborted turns rather than treating their output as a new successful tool loop. Failure remains an explicit outcome.
- [Codex interruption context at f752c97](https://github.com/openai/codex/blob/f752c97f7d0be0cbef5cece327f52a16ad6fb1c6/codex-rs/core/src/context/turn_aborted.rs) preserves uncertainty about interrupted tools and background work. Cancellation does not establish rollback or authorize replay.

The local `knowledge_state.validate_failure` contract already requires `status=error` with empty answer, claims and evidence. `KnowledgeSessions.fail` enforces it. This patch makes a normally returned workflow error use that existing boundary too; it does not reinterpret `partial`, `abstained`, `clarification` or legitimate internal diagnostic events as failure.

## Actual reproduced path

At formal base `c8ef7582f226c07f1c06d3874dba159708ca7fad`, a tool exception is recorded by `KnowledgePlanning.tool_node`, which returns a normal dictionary with `halted=True`. The graph therefore legitimately follows tool → compose → deliver → complete. Compose marks the business result `error`, but delivery includes its diagnostic answer and current-turn evidence. `KnowledgeTurn.run` formerly treated every returned graph result as successful publication: it updated `last_answer`, emitted an assistant event and called `SessionStore.finish`.

The repro-only commit `caf6da0c449ba1f17d3022c8979ff93ff5bdcc86` retains unchanged production source and four deterministic contracts. Its actual run had five assertion failures, including both immediate tool failure and a successful retrieval followed by a second failed tool. The latter returned the synthetic original text and retrieval details despite the error result. This is a failure-response contract violation, not evidence that an unauthorized principal obtained those documents.

The fixtures execute the real `KnowledgeService`, `KnowledgeWorkflow`, Python `GraphProgram`, native Rust graph/kernel and SQLite session store. Only model completions and search/authority inputs are controlled synthetic substitutes. No model, embedding or judge provider is called. These are not PostgreSQL, HTTP or production quality measurements.

## Minimal correction

Immediately after runtime completion and the existing execution check, `KnowledgeTurn.run` checks exactly `response['status'] == 'error'`. It raises the existing `PlatformError` with the original `stop_reason`, before any success-state construction or `finish` call. The existing exception handler removes response tool arguments/results and derived retrieval/verification details, returns empty answer/claims/evidence, and invokes `store.fail` once.

Durable internal tool receipts and their original statuses remain available. A successful tool may have run before cancellation or a later failure; the change neither erases that receipt nor automatically replays it. Repeating the same request returns the persisted, redacted failure without another model/tool execution. Earlier valid session history is not globally deleted, and this patch makes no claim that internal audit artifacts contain no document data.

## Mandatory lint and actual contracts

`scripts/knowledge_workflow_checks.py` uses Python's standard AST. The ordinary and formal registry gates require the direct post-runtime execution check and exact error-only guard to dominate the single success publication exit. They require the existing redacted failure exit, reject replacement/mutation of the classified response, and preserve the original failure code. This is a deliberately narrow structural rule, not a general interprocedural effect or alias analysis.

The shared `session_commit` source anchor for `KnowledgeTurn.run` is explicitly rereviewed: only routing a business error into its existing failure transaction changes. Session-store transaction, lease and writer-fencing code is unchanged; the session owner's coverage status is not promoted by this workflow model.

The contract suite checks ten bad AST variants after refreshing all affected source hashes: removed, inverted, overbroad, late or swallowed guards, changed reason, replaced response, leaked failure answer, successful publication from the failure handler and double failure publication. The mandatory-registry fixture uses the entire actual source module with an isolated inventory so unrelated model families are not rescanned for each mutant; its unchanged positive control must pass. Equivalent AST formatting is accepted. A guard-removal implementation mutant executes the same four real workflow contracts and must reproduce the five failures; the source hash is not their proof.

Existing graph contracts remain intact: source selection removes previous evidence from planner input/schema, stale citations are rejected, semantic verification repairs at most once and reverifies, partial/abstained results remain legitimate, budgets prevent extra exploration, and permission changes redact delivery. Additional contracts cancel after an actual tool result and change authority after actual graph completion, demonstrating that terminal graph state alone is insufficient for answer publication.

## Explicit limits

The [finite model](../formal/knowledge_workflow/README.md) is partial coverage, not code refinement. Semantic verification repair is distinct from bounded local output-format/citation repair and abstention review. The model and regressions do not prove semantic answer correctness, arbitrary model output behavior, or unlimited execution domains.

Execution and authority are checked at identified synchronous boundaries. They are not atomically coupled to external database commit or eventual HTTP response delivery. Authority may change after a check; external DB isolation, durable acknowledgement loss, process death, session-writer replacement and provider billing remain separate owners/obligations. Cancellation after a side effect does not undo it. BaseException interruption retains the existing unresolved-session behavior rather than being mislabeled a known failure.

`ingestion_migration` remains an independently unmodeled inventory family; it is not included in this patch.
