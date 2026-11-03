# Native multiblock assembly owner

This adds a finite model and deterministic native contracts, not a claimed production bug fix. It complements (does not replace) the one-block usage/task-ownership model in `llm_stream`.

Reference first: Anthropic's official `anthropic-sdk-python/src/anthropic/lib/streaming/_messages.py` uses the event's block index for delta/stop lookup and keeps per-index JSON buffers. OpenAI's official `openai-python/src/openai/lib/streaming/chat/_completions.py` accumulates indexed tool-call deltas. These are independent provider-specific accumulation precedents, not proof that their malformed-input policies equal ours. No vendor source was copied.

- https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/lib/streaming/_messages.py
- https://github.com/openai/openai-python/blob/main/src/openai/lib/streaming/chat/_completions.py

The repository owns a stricter clone-validate-commit assembler. The model retains two indexed blocks, two tool identities, insertion order, complete/incomplete argument abstraction, closure, terminal finish kind, and response/replay filtering. Start plus delta represents implicit allocation; `BlockEnd` payload replacement and numeric/string domains are covered by native contracts rather than represented byte-for-byte. Rejected-event observation abstracts the candidate snapshot discarded by `push`.

The current implementation keeps both nonblank text and nonblank reasoning on an error/abort. Reasoning is not treated as an executable tool. Successful `MaxTokens` removes tool calls and applies the identical mask to per-block replay metadata. The opaque envelope response field remains adapter-owned. A malformed per-block replay length discards replay; an absent per-block list remains opaque. Arrival order, not numerical index order, is the content/replay order.

Four seeded unsafe models remove rollback, executable tool-ID checks, replay mask alignment or replay length rejection. Four deliberately false witness invariants demonstrate distinct tool IDs, filtered content, a rejected closed block and nonnumeric arrival order. These are test mutants and reachability probes, not newly discovered implementation bugs. Ordinary native contracts exercise real public `Assembler` operations; mandatory registry lint rejects semantic regressions even when source hashes are refreshed.

Bounds and exclusions are in the registry. This is finite safety checking, not unbounded equivalence or provider/transport correctness. No paid/provider calls are made.
