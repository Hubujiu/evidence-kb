# Physical request settlement: bounded model and concrete regression

This is a **partial**, finite safety model, not a refinement proof for the whole provider stack.
It describes the reservation/send/settlement boundary and conservative no-replay receipt authority.
It does not prove RAG quality, provider correctness, production durability, or eventual recovery.

## Mature precedents inspected before selecting the fix

- [SQLite transaction semantics](https://www.sqlite.org/lang_transaction.html#implicit_versus_explicit_transactions): `COMMIT` returning `SQLITE_BUSY` can leave the transaction active. Other failures can roll back some or all work. Therefore an exception is not, by itself, a durable rollback receipt. We do not add a SQLite commit retry loop or change driver policy.
- [FoundationDB developer guide at `bcdefac96277de4a26d59cdf352d478391d347f9`, lines 563–598 and 922–962](https://github.com/apple/foundationdb/blob/bcdefac96277de4a26d59cdf352d478391d347f9/documentation/sphinx/source/developer-guide.rst#L563-L598): distinguish known failure from unknown commit knowledge; retries require suitable idempotency. Its documented no-longer-in-flight guarantee for `commit_unknown_result` is **not** assumed for arbitrary SQL/wire errors in this project. The model includes decided commit/rollback branches; late commits remain an explicit modeling obligation.
- DeepSeekHarness `ce7073d51254c26cd63b22a60835b8f948deb447` was considered for provider failure/retry/token-meter behavior. It is not evidence for durable budget or database commit ownership. No source code or TLA+ module was copied from these projects.

## Concrete bug and repair

The old `MeteredTransport._handle_admitted` and `GoogleSemanticProvider._observed` caught a settlement failure as if it were a new provider failure. They attempted a second `BudgetLedger.finish` for the same physical identity. If the first update committed but its acknowledgement was lost, the STARTED-only compare-and-set rejected the second update. The resulting terminal-row error replaced the original `CommitUnknown`, including its operation identity. Metered transport also left its capture at STARTED and omitted the received response/usage.

Both owners now establish the observed provider outcome before entering settlement and never call settlement again after it has begun. Captures distinguish:

- `status`, response facts and `usage`: observed provider outcome, not proof of a budget transaction commit;
- `ledger_settlement=UNCONFIRMED`: the caller has no successful settlement acknowledgement; the durable row may still be STARTED or already terminal;
- `ledger_settlement=COMMITTED`: `finish` returned successfully;
- `ledger_error` and, when available, `ledger_operation_id`: the original failed operation.

No re-read is fabricated, no unknown reservation is released, no physical request is resent, and no automatic reconciliation is introduced. The original exception object, code, operation identity and cause are preserved. If the final capture write also fails, that failure is attached as an exception note rather than replacing the settlement failure. The prior capture may remain STARTED or be absent; successful capture persistence cannot be promised when storage fails. File writes are not a new fsync/directory-durability protocol.

## Abstraction and source mapping

One model request index pairs the provider receipt identity with the budget identity; their actual UUID strings need not be equal. One logical request admits at most two explicitly linked physical attempts. A producer guard serializes admission. A crash can occur at each modeled boundary and preserves durable ledger/receipt state.

| Model action/state | Implementation boundary |
|---|---|
| `BeginReceipt`, receipt STARTED | `RequestReceipts.begin`; database-backed `ReceiptDatabase.begin/_locked` |
| `Reserve(ACK/ROLLBACK/LOST_*)` | `BudgetLedger.reserve/_reserve`, SQL transaction exit |
| `Send`, `reservedAck` | `MeteredTransport.inner.handle_request`, Google `remote._post`, only after `reserve` returns |
| `Observe` | provider HTTP result/transport unknown and bounded actual usage |
| `BeginSettlement`, `Finish` | owner-local `settle`; `BudgetLedger.finish` STARTED-only update |
| `HandleSettlementFailure` | preserve observed outcome and propagate original error; legacy flag represents second finish invocation |
| `FinishReceipt` | outer provider receipt finalization; an unresolved failure conservatively retains UNKNOWN authority |
| `Crash`, `Restart` | process loss across boundaries, not rollback of durable state |

The negative model stops at the second settlement invocation. It does not need to simulate the rejected SQL statement's entire execution. Its legacy lost-commit trace is:

1. Begin receipt, then acknowledge durable reservation.
2. Send exactly once and observe COMPLETE with actual usage.
3. Begin settlement and durably write COMPLETE; lose its commit acknowledgement.
4. Legacy handler invokes settlement again, violating `SettlementAtMostOnce`; the already-terminal row replaces `CommitUnknown` with `TerminalRowError`.

## Checked bounds and results

`PhysicalBudget.cfg`: two physical requests, reservation 2, actual known usage 1, cap 4, one crash/restart, all four settlement results (acknowledged commit, definite rollback, committed/no acknowledgement, rolled-back/no acknowledgement). `RetrySettlement=FALSE`.

TLC 1.7.4, Java 21, one worker, 256 MiB heap: **211 generated / 193 distinct states**, depth **16**, no invariant violation. The result applies only to those bounds and assumptions. No liveness/fairness property is claimed; receipt UNKNOWN intentionally can block forever without external reconciliation.

`PhysicalBudgetLegacy.cfg` restricts settlement to committed/no acknowledgement and reenables the legacy second invocation. TLC exits **12**, reports **`SettlementAtMostOnce`**, and produces the eight-state trace above (41 generated / distinct states). A parser error or timeout is not acceptable negative evidence.

Run from the repository root with the pinned jar supplied by the formal gate:

```sh
java -Xmx256m -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -cleanup -config PhysicalBudget.cfg formal/physical_budget/PhysicalBudget.tla
java -Xmx256m -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -cleanup -config PhysicalBudgetLegacy.cfg formal/physical_budget/PhysicalBudget.tla
```

`physical_settlement_contracts.py` supplies synthetic, offline tests using real local SQLite commit/rollback operations. They inject acknowledgement loss **after** a real commit and definite failure **before** transaction exit. They also check database receipt begin/finish no-replay behavior and audit-write failure. They do not simulate a paid provider, actual network loss, host power loss, or an arbitrary backend's durability implementation.

## Remaining obligations and limitations

- Provider-receipt and budget transactions are not one atomic transaction; this model does not prove their full cross-store recovery protocol or all generated identity mappings.
- Late/in-flight commits, backend-specific isolation and active SQLite `SQLITE_BUSY` transactions are not modeled. Caller-side no-second-settlement remains conservative, but that is not a proof of those backends.
- File-backed receipt concurrency, stale finish/reconcile races, fsync and directory durability need separate modeling.
- Budget batch lineage, rollover/review of unknowns, lease expiry, multiple concurrent producers and operator reconciliation are excluded.
- Multiple resource dimensions, partial usage and actual usage greater than reservation need a larger accounting model. `AdmissionBounded` here relies on known usage not exceeding the modeled reservation; production intentionally reports an over-budget actual rather than erasing it.
- Provider admission, 403 acknowledgement flows, retries after known connection failures, format-correction retries, cancellation and all validation/decode errors are not covered end to end.
- The model has no theorem proving arbitrary request counts, no fairness assumptions, and no claim that source hashes establish semantic refinement.

## Per-fix structural lint

`physical_settlement_lint.settlement_violations(root)` is the mandatory-gate hook for these two owners. It parses Python ASTs and enforces a deliberately narrow architecture: a single local settlement helper, an UNCONFIRMED marker before the only physical finish call, unconditional bare rethrow on helper failure, confirmation only after a normal return, and an outer exception handler that immediately rethrows once settlement has begun. It also rejects direct duplicate finish calls, looped settlement and explicit marker erasure. Contract mutants exercise both owners; ordinary formatting and unrelated owner edits remain accepted.

This is a structural regression guard, not general Python control-flow verification. Arbitrary dynamic dispatch, monkeypatching, aliased callees or malicious reflection are not proven safe by it. The concrete fault-injection contracts and finite model remain separate requirements. `reservedAck`, observed response facts and counters in the model are history/ghost observations, not a claim that process-local memory survives restart or that captures were fsynced.

## Capture consumers are part of the settlement boundary

Independent review of the first repair found and reproduced a regression: after a **real SQLite rollback**, the new capture correctly retained provider COMPLETE and observed usage, but the old capture-only workflow guard and summary looked only at `status`. They accepted the record as settled while the durable budget remained STARTED. The underlying ledger's admission block was a backstop, not justification for that false workflow claim.

`capture_unresolved` now separates those dimensions. A nonterminal provider status is unresolved. A capture that explicitly contains `ledger_settlement` is also unresolved unless that value is COMMITTED. Records written under the existing capture contract before that optional field was introduced still use their original status semantics; there is no conversion, rewritten history or inferred commit acknowledgement. Explicit UNCONFIRMED or invalid marker values never fall back to the older interpretation.

Delivery preparation/next-case guards, delivery summaries and final bindings, SDK recovery caching/retry admission, reader diagnostics, PageIndex probe and full-query bindings all consume this predicate. Final binding checks reject any unresolved capture in the bound operation, not just the chosen final response. Summaries retain actual observed tokens independently and expose `unconfirmed_settlement_requests`; unknown `usage=None` remains unknown rather than crashing aggregation or becoming zero. Embedding recovery already independently rereads the ledger and matches physical status/usage. Budget predecessor freezing and reviewed recovery already read the durable ledger and reject STARTED/UNKNOWN as appropriate.

`CaptureSettlement.tla` models the concrete observation → settlement → persisted capture → workflow-check boundaries, including a crash before capture publication. With acknowledged commit, definite rollback and both lost-acknowledgement outcomes, the fixed model checks **22 generated / 21 distinct states**, depth **5**. `CaptureSettlementLegacy.cfg` reenables provider-status-only acceptance after a definite rollback: TLC exits **12** at `NoWorkflowReleaseWithoutConfirmedSettlement`, with a five-state counterexample (10 generated/distinct states). Provider COMPLETE remains true throughout; the violated fact is workflow release despite an UNCONFIRMED settlement and durable STARTED row.

Real-SQLite regression contracts execute those consumers after both committed/no-acknowledgement and rolled-back settlements. SDK recovery tests verify no cache admission and no HTTP transport construction for unconfirmed terminal records. Structural-lint mutants remove each of the seven registered consumer gates or replace the shared predicate with status-only checking; each is rejected. This finite companion model does not establish arbitrary recovery graphs, user edits to capture files, or cross-store atomicity.

A directory-only guard still cannot establish capture completeness when the initial capture write produced no file at all. That pre-existing observation limit is not repaired by a predicate over present files; the durable ledger remains the admission authority. PR #31 separately preserves known-zero-send facts for that pre-send failure without erasing its STARTED reservation. No change here claims those two authorities have become one atomic store.
