# Database-backed receipt transactions

This adds a bounded model and actual SQLite contracts for `ReceiptDatabase` and
`RequestReceipts(database=...)`. No production implementation is changed and no
new product defect is claimed. PR36's [file model](../file_receipts/README.md)
remains separate: a pair of SQL rows commits atomically, unlike split file
publication, pending unlink and terminal marker operations.

## Mature mechanisms and actual scope

The existing implementation uses a write transaction, logical-row `FOR UPDATE`
and a structured-dataset write lock. SQLite additionally starts `BEGIN IMMEDIATE`;
its lack of a rendered row-lock clause is not absence of synchronization.
[SQLite transactions](https://www.sqlite.org/lang_transaction.html) describe
single-writer admission and rollback/commit boundaries.
[SQLAlchemy's select documentation](https://docs.sqlalchemy.org/en/20/core/selectable.html#sqlalchemy.sql.expression.Select.with_for_update)
explains the explicit locking clause, while
[PostgreSQL row locks](https://www.postgresql.org/docs/18/explicit-locking.html#LOCKING-ROWS)
last until the transaction ends. PostgreSQL/MySQL execution is not claimed by
these SQLite contracts. Database engine atomicity and correct driver/lock
implementation remain trusted below the receipt model.

`_Transport.__init__` still selects file receipts by default. The send-order
contracts deliberately inject a real database-backed receipt implementation into
the actual `_send_attempt` method with a synthetic response function. This is an
interface-composition check, not evidence of a default production SQL route or
observed production outage.

## Mapping and linearization

`DatabaseReceipts` bounds two logical IDs, two fresh physical IDs and two callers.
A dataset-wide writer owner matches the actual structured lock (and SQLite's
writer exclusion). `StartBegin` / `StartTransition` acquire it and read the
committed maps. `Check` validates unresolved/terminal predecessor, retry link,
format-correction reason, current physical owner, logical origin and explicit
reconciliation evidence. Rejected calls change no durable receipt data.

`WritePhysical` then `WriteLogical` change only the transaction's staged maps.
`Commit` publishes both together and releases the writer lock. This is the
linearization point for durable receipt state, **not** caller acknowledgement.
`Acknowledge` and `Send` are separate transitions. An interruption after commit but
before acknowledgement retains STARTED while this invocation has not sent HTTP.
After reopening, an arbitrary STARTED row is still unresolved: its status alone
cannot establish whether another pre-crash invocation sent anything.

`AbortOrRestart` releases a held transaction and discards staged writes, or loses
an acknowledgement after a committed transaction without undoing it. Durable
latest mappings and all physical history remain. `settled` and `rejectedStale`
are specification-only observation history, not storage columns. Reconciliation
is an explicitly evidenced terminal transition; it does not refund an independent
UNKNOWN spend ledger, prove provider cancellation or authorize an automatic retry.

Safety invariants check logical/physical status agreement, unresolved coverage,
one unresolved physical row per logical ID, logical-origin isolation, immutable
terminal history and no send without an acknowledged begin. Five deliberate
fault controls split the pair commit, omit transition CAS, admit a retry over
UNKNOWN, send before acknowledgement or omit logical/current-owner isolation.
Three additional **reachability witnesses**, not bug controls, establish normal
acknowledge/send, committed-unacknowledged STARTED without send, and released
stale rejection. Witness configurations intentionally negate reachable states;
the expected TLC invariant failures are positive reachability evidence.

## Representation reduction and retained timeout

The first unnormalized model timed out at the unchanged 120-second limit. Its
last recorded progress was 11,416,227 generated and 975,603 distinct states; this
was incomplete exploration, not a PASS or counterexample. The original input
snapshot and log are retained in the development artifact. Reducing dead local
state kept the same two/two/two bound, actions, durable states and safety checks.

| Cleared local values | Last relevant reads and preserved values |
| --- | --- |
| `logical`, `outcome`, `retryOf`, `reason`, `evidence` at commit | Used only by pre-commit validation and staging; commit's terminal history reads `stagedStatus`, not these cleared values |
| `operation` after acknowledgement | Kept through committed state to distinguish begin acknowledgement and the pre-ACK negative control; normal SendReady needs only physical identity |
| `physical` after Send or a non-begin acknowledgement | Kept through both begin acknowledgement and Send; only cleared after it has supplied their identity |
| All caller locals on abort/rejection/restart to Idle | Every new invocation assigns its inputs; Idle guards use only pc, writer ownership and remaining fresh IDs |
| Staged maps on abort by their owner | Restored from committed maps only when `owner = a`; another caller's currently held transaction is never reset |

`nextId`, committed row maps, terminal history, acknowledged IDs and sent IDs are
never reset by this normalization. ACK-loss state therefore remains distinct
from an acknowledged send. The stale-rejection witness adds only a Boolean
observation; it never enables an action. This is a documented representation
reduction, not a Python refinement theorem or product repair.

## Actual contracts and remaining obligations

Eight new contracts use real SQLite connections and two reopened handles: racing
begin and transition calls, uncommitted-read isolation, real SQLite trigger
failures between physical and logical writes, post-commit lost-ack injection,
wrong logical/dataset and stale physical refusals with unchanged row snapshots,
retry policy, explicit reconciliation and unchanged independent UNKNOWN budget.
The ACK-loss fault is injected after a real successful commit; it is not a claim
of reproducing a network disconnect in SQLite. All external sends are synthetic.

Structural AST guards check the shared transaction, locked read, physical/logical
save, validation-before-save, acknowledgement return order, CAS predecessor and
logical checks, explicit evidence and transport send ordering. Refreshed-source
hash mutants must still fail mandatory formal lint, reached by both the ordinary
and additional formal project gates. These guards are targeted checks, not a
complete semantic equivalence prover.

Unbounded IDs, UUID/hash collision, arbitrary hostile metadata overwriting
reserved receipt fields, correct operator-evidence content, network/database
failover, cross-process crash durability, SQL isolation implementations,
provider exactly-once delivery and liveness are not proved. The logical-ID bound
checks origin separation within one dataset; actual contracts additionally check
separate datasets. Receipt resolution does not model or alter physical budgets.

## Recorded verification

The [integrated summary](evidence/integrated/verification.json) records 19/19 TLC
runs and two evidence contracts passing. The database positive explored 1,376,377
generated / 381,963 distinct states, queue zero, in 22.419 seconds. All five
fault controls and all three reachability witnesses produced their exact expected
invariant failures. [Focused contracts](evidence/focused-contracts.log) passed
43 tests, including the eight actual SQLite contracts and refreshed-hash semantic
lint mutants. This is not ordinary full-project acceptance; that gate is still
required separately.

Historical evidence retains the initial timeout, the normalized development run,
fault controls, and a failed witness-development run (exit 75 from a wrongly
grouped Boolean RHS). The latter is explicitly FAIL, not a valid witness. The
corrected current assignment groups the entire Boolean expression. Historical
model snapshots use `.tla.txt` so the live runner does not treat old modules as
current inputs. Each stage's standard `SHA256SUMS` contains the original input
digests, not recomputed current-model digests; every compact result field, copied
raw log and checksum was compared with its unmodified live-run artifact.


## Ordinary project gate on frozen c81c8ba

The one ordinary gate reached a real **FAIL**, exit 1; it was not incomplete.
[The recorded summary](evidence/ordinary-project/summary.json) and unmodified
driver log preserve every check. Eleven checks passed, including Rust build/test,
plugin-process, foundation and host integration. Contracts ran all 1,086 tests
with complete discovery, zero assertion failures, 18 errors and zero skips.
Every error was the same HTTPX missing-`socksio` ImportError in this environment.
All eight new SQLite contracts and both new lint contracts passed inside the
ordinary four sealed workers. No proxy setting, dependency or gate threshold was
changed and no full run was repeated. This failure is not an acceptance PASS;
the independent draft PR still requires its exact complete CI.

This evidence-only addition changes no model, Python/Rust source, registry or
gate from c81c8ba. Original full logs remain in
`.local/receipt-database-full-20261102-2143`; the tracked per-artifact SHA256SUMS
refers to those original bytes. It does not claim a new production defect.
