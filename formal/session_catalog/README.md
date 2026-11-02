# Session catalog and durable adapter slices

This is a **model-only** extension to PR35's session feature. No runtime defect or production fix is claimed. The existing SessionWriterLease and PostflightAuthority inputs, independent cross-store limitations, and their historical evidence are unchanged.

## Mature patterns reviewed first

- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): a database transaction publishes all its writes or none; commit acknowledgement is a separate caller observation.
- [SQLite isolation](https://www.sqlite.org/isolation.html): separate connections see committed changes; the actual provider uses BEGIN IMMEDIATE for writes.
- [PostgreSQL row locks](https://www.postgresql.org/docs/current/explicit-locking.html): the existing owner SELECT FOR UPDATE maps to transaction-scoped row serialization. No new PostgreSQL execution is claimed by these SQLite tests.
- [AWS Builders' Library idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/): operation identity and mutations must be recorded atomically, and ambiguous replies cannot establish that an operation did not happen.
- [Stripe API v1 idempotency](https://docs.stripe.com/api/idempotent_requests): saved terminal results and parameter matching are a useful mature comparison for turn replay, not a claim that this application's create endpoint is idempotent.

## Stage 1: SessionCatalog

Source boundaries: SessionStore.create, _owner, load, list_sessions, events, artifact, read_artifact, canonical/digest, and the session/artifact schema. Creation is staged -> committed -> caller acknowledged or unknown. An unsuccessful response after commit can leave an owned durable session. There is no client create idempotency key, so the model does **not** promise retry deduplication for create.

Finite scope: 3 actors distinguish a same-tenant different principal from a same-principal different tenant, 2 fresh session identities, 2 distinct canonical bodies, and 4 session/body artifact keys. UUID freshness and digest collision resistance are assumptions, not proofs. Artifact writes consume the already reviewed valid-writer-lease boundary; this does not remodel lease takeover.

Reads snapshot owner and content validity at the read transaction. A later storage fault does not retroactively invalidate that observation; no response-instant authority/integrity claim is made. The corruption action is an explicit external faulty-storage input, **not** a transition supported by the append-only API. The source provider rejects normal UPDATE/DELETE of artifact rows.

Invariants cover acknowledged creation durability, artifact parent existence, both owner identity components, session-scoped artifact lookup, checked content, and writer admission. Five seeded variants independently bypass tenant, principal, content, session-key, and lease checks. Three additional controls are terminal reachability witnesses (unknown create ACK with a durable row, actual dedup, and an authorized artifact read), not runtime defect claims. The grouped dedup Boolean RHS and exact positive-behavior witness registrations have mandatory reviewed-shape checks.

### Actual SQLite contracts

Six new tests use the real public SessionStore methods and isolated temporary SQLite databases: precommit rollback versus real commit followed by synthetic lost ACK; wrong actor reads and every mutation leaving all four tables unchanged; two-client concurrent artifact dedup; same hash not granting another session access; UTF-8 canonical digest/size and returned-value isolation; artifact trigger rollback, ACK loss and reopen/dedup; malformed stored content rejection; and expired writer rejection.

The initial six-test attempt had five PASS and one fixture ERROR: trying to manufacture corruption with UPDATE hit the existing DATABASE_APPENDONLY trigger. That original failure is retained. The corrected fixture first asserts this protection and then seeds a separate malformed initial artifact row through the structured transaction fixture API. It does not remove triggers, relax the guard, or claim normal writes can corrupt artifacts. Both read and existing-artifact put then reject the mismatched body/hash without changing tables.

The mandatory semantic source guard is deliberately a narrow reviewed AST-shape contract for these methods. Stage 1 has fourteen hash-refreshed source/model/registration mutants; stage 2 adds a fifteenth checking the default locked owner query. They must fail; hashes alone are not treated as sufficient. It runs through registry_violations in both ordinary formal-lint and --formal-only, using the existing project entrypoints.

## Evidence and honest scope

Stage evidence contains transparent compact summaries, raw TLC/contract logs, and the **original** source-input SHA256SUMS for that stage. Live runner JSON remains unchanged under the corresponding .local artifact; it is not duplicated into Git. Reachability uses the same positive-model behavior and intentionally fails its negated witness.

Stage 1 targeted TLC: 9 expected outcomes PASS; positive 416,381 generated / 18,554 distinct states, empty queue. Integrated mandatory formal entry: 22 expected outcomes PASS + 2 separately OBSERVED_LIMITATION + 3 evidence contracts PASS. Targeted actual/lint/existing session contracts: 21 PASS. Full ordinary acceptance remains a separate required final-tree operation, not replaced by these focused checks.

Additional adapter publication, cached replay and uncertain finish acknowledgement are covered by the bounded stage 2 slice below. Unbounded sessions/history/content, list-page ordering under concurrency, migration, filesystem/power-loss durability, hash collisions, arbitrary identity mutation, storage-grant ABA, and cross-store authority-at-commit are not proven here. The existing PR35 limitations stay separate.


## Stage 2: DurableAdapter

Source: DurableSessions.create/load/history/begin, DurableSession initialization/start/messages/append/finish/fail, _persist, _pending_tools and the execution checkpoint. The existing SessionStore transaction/fencing contract is a dependency, not reimplemented or weakened. Actor ownership remains the separate catalog boundary; namespace checking precedes this adapter admission.

The finite record represents one original turn, one same-request cached replay, one new-turn attempt while a tool result is pending, one tool effect, two namespace validity cases, and a cancellation checkpoint. It separates final-answer staging, durable commit, acknowledgement, adapter return, and local closure. Its `public` field observes the adapter boundary, not eventual network delivery. The `candidate` Boolean records the original turn's local final staging; after closure/replay it is not consulted again. Its abstract clearing on fresh completion/failure is a dead-local normalization, not a claim that Python clears the private `_final` reference. No arbitrary concurrent reuse of one in-memory adapter instance is assumed.

The existing transaction atomically commits final assistant event, state/turn count, terminal event and saved response. Lost finish ACK leaves the database committed while the local object remains open; cleanup must reject the already terminal request. A new same-request instance can replay the saved result without re-executing the tool. A recorded tool call without a recorded result blocks new turns even when this particular invocation knows it sent zero effects. Observed tool-effect counters are retained; this slice has no refund action and does not establish independent budget-ledger accounting.

Cancellation is checked before finish. Cancellation arriving after that check can coexist with commit; this model does not invent response-instant cancellation atomicity. The separate PR35 cross-store authority-at-commit counterexamples remain unchanged.

Five fault controls bypass namespace, close before ACK, rewrite committed success during cleanup, re-execute a cached effect, or bypass the unknown-tool fence. Eight terminal observation witnesses cover success, durable success with unknown ACK, cached success, a pending-tool new-turn refusal, tool-record ACK loss with zero actual effects, cancellation rejected before the checkpoint, **replay specifically after ambiguous commit**, and **same-request error replay with an unknown prior tool effect**. The last two strengthen composition evidence without changing Init/Next: the preliminary six-witness model already reached 96 states; adding pure observation predicates does not alter that behavior.

Seven new actual SQLite/NativeLoop contracts pass: direct and public finish ACK loss with exact logical-row snapshots before/after cleanup; cached replay retaining model/tool counters; tool-call-record ACK loss with zero dispatch; genuinely unknown tool result with one dispatch; user-record ACK loss before any model invocation; copied final/history buffers; and original BaseException identity preserved through real failure cleanup. Real commit occurs before each injected ACK loss; these are not mocks claiming an unperformed commit succeeded.

Mandatory AST-shape guards preserve the reviewed adapter error/copy/close/admission operations even after source hashes are refreshed. Seventeen adapter mutants plus fifteen catalog mutants are rejected. Full source-mapping lint runs through both existing project modes. These narrow shapes require deliberate review when implementation changes; they are not a parser or a refinement proof.

Preliminary combined checks: 34 expected formal outcomes + 2 separately observed limitations + 3 evidence contracts PASS; 38 actual/lint/existing session contracts PASS. Two explicit composed witnesses were then added with no behavior change, yielding 36 expected formal outcomes. The original stage 1 summaries/hashes/logs and the initial fixture error remain unchanged. Final ordinary gate status is reported separately after the final source tree is frozen.


## Frozen-tree ordinary required gate

One actual ordinary run on frozen source 779a1ca (tree 826f66770c4a71409754870fc01929cd98334b6a) exited 1: **FAIL**, not overridden by targeted/formal successes. Ten of twelve gates passed. The host child printed 25 tests OK in 178.762 seconds but did not exit within the required 180-second deadline, so the host gate correctly remains FAIL. Contracts discovered all 118 nonempty modules / 1,091 tests: zero assertion failures, 18 errors, zero skipped/expected failures. All 18 original tracebacks end with the HTTPX missing-socksio ImportError; all 17 added catalog/durable actual and semantic-lint contracts passed inside the sealed ordinary workers.

The original driver/host logs, exact error identities, compact summary and original-output SHA256SUMS are retained. No proxy changes, dependency installation, threshold relaxation or retry was performed. This evidence-only append changes no source/model/config/registry/gate file from the tested tree. The first-stage PR58 CI passed and was fully audited, but does not substitute for fresh final-head CI.
