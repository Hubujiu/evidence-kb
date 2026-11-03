# Application and turn reservation accounting

`agent_model.BudgetLedger` is the in-memory application/turn ledger, not the
SQLite physical-request ledger and not DSH's token-pressure measurement. The
[mature boundaries](../budget_batches/README.md#mature-boundaries-not-transplanted-algorithms)
remain separate: this model neither replaces physical request identity nor
turns an estimate into provider billing evidence. No product defect or product
implementation change is claimed in this stage.

## Finite model and linearization

`TurnReservations` has three one-shot invocations and two turns. One invocation
may attempt twice; the other two may attempt once. The global call cap is three,
each turn cap two, token admission cap six, per-attempt reservation two and known
last-response usage zero or three. The latter intentionally can exceed the
estimate. Each invocation has complete session/turn context and a fixed provider
attempt bound; standalone calls without turn context are outside this instance.

| Model transition | Actual implementation |
| --- | --- |
| `Cache` | Application cache branch before either reservation; response attempts/usage are zero |
| `ReserveTurn`, `DenyTurn` | Shared `(session_id, turn_id)` ledger and its atomic reserve |
| `ReserveGlobal`, `DenyGlobal` | Global reserve, or `except BaseException` releasing only the newly held turn reservation with zero attempts |
| `Attempt` | Provider-owned bounded physical-attempt trace while both reservations are held |
| `Result`, `Unsent` | Known/unknown usage or a zero-attempt preflight failure; downstream failure does not un-observe usage |
| `SettleGlobal`, `SettleTurn` | Two distinct `finally` settlements: global first, then turn |
| Known/unknown counters | Latest known usage plus a full conservative charge for every earlier unobserved attempt |

Reservation changes linearize inside each ledger's own lock. There is no atomic
snapshot across global and turn ledgers: after global settlement, another
observer may legitimately see the turn still reserved. The model compares
completed invocations only, and an actual regression observes this intermediate
state before confirming final agreement.

Safety checks require both reservations before a send, no abandoned reservation
at a terminal return/refusal, retention of known usage and all unobserved attempt
charges, matching completed ledgers, zero physical cost for an application cache
hit, and bounded physical call admission. Five seeded controls omit global
reservation, omit rollback, discard observed usage on error, refund unobserved
attempts, or count a cache hit as a physical attempt. Each isolates its named
invariant. No liveness property is claimed.

There is deliberately **no** invariant requiring reported token cost never to
exceed the admission cap. A provider can report more than an estimate after the
physical request happened. Settlement must preserve that cost and block later
admission as appropriate, not erase usage to make a bound appear satisfied.

## Actual contracts

New tests exercise real `AgentModel` and both real ledgers for global call/token
refusal, rollback without releasing another global owner, propagating provider
cancellation, full-budget cache reuse, exact known/unknown accounting after
validation/cache/diagnostic-record failure, transient two-ledger snapshots and
repeated settlement rejection. The cache/record failures retain their original
exception objects; canonical PR31 error normalization is not replaced.

The small retry fixture supplies two synthetic attempt observations to the real
model; it is not a claim of two HTTP transmissions. Existing
`TurnBudgetContracts` separately exercises the actual mocked HTTP retry/rerank/
format-repair composition and shared turn cap. `StructuredBudgetContracts`
checks public model-client budget projection and replay without another send.
All provider traffic is synthetic; there are no paid model or judge requests.

## Assumptions and remaining obligations

Provider attempt reporting and fixed `max_attempts` are trusted; the attempt
trace stops changing after provider completion. Input validation
and `_usage` normalization precede ledger settlement; the ledger's locked
settlement itself is assumed to finish without an internal failure. Arbitrary
asynchronous interruption inside cleanup, abrupt process death, durable restart,
unbounded turn retention and reservation-ID collision are not proved.

Only bounded cost classes and two turns are explored. This is not a capacity-20
induction or Python refinement proof. Exact monetary billing, arbitrary resource
dimensions, changing per-turn limits, malformed provider traces, independent
calls lacking turn context and full RPC/error-event propagation remain outside
the finite model. Existing actual contracts still test limit conflicts and
conservative handling of invalid attempt counts. Other state-machine families
remain explicitly partial or unmodeled.

Evidence uses compact historical summaries, per-run standard SHA256SUMS and raw
TLC logs. The live runner's complete JSON schema is unchanged. Earlier targeted
runs and any development failure logs remain historical records, not substitutes
for the final ordinary and additional formal gates.
