# Discovered local lifecycles

These bounded safety models cover three actual transition owners missed by the original state/status/phase naming scan. They are partial abstractions, not implementation refinement. The ordinary gate runs `discovered_lifecycle_contracts`; the formal gate uses the existing pinned SANY/TLC runner and registry. `discovered_lifecycle_lint` checks reviewed source seams even when source hashes are refreshed.

| Model | Source owner | Actions and limits |
| --- | --- | --- |
| MemorySession | `harness_sessions.py:MemorySession` | Two append actions, one independent message snapshot, finish and fail each at most once. Closing clears owned records and refuses further append; repeat close is harmless stuttering. No durable state or restart guarantee. |
| RawSqlLifecycle | `storage_adapters/sql.py:_SqlSpace` | Two prepares and two physical transaction-entry attempts. Prepare rejects after explicit or implicit seal. Invalid mode preserves admission. Valid transaction seals before physical entry even when entry raises. A prior prepare is **not** required by this raw wrapper. Physical schema preparation and database atomicity are separate owners. |
| DeliveryControl | `scripts/delivery_control.py:start/stop` | One suspended child, acquired identity handle, published receipt and gate release; exact profile/instance liveness; one stop request, identity/private marker validation, observed exit or timeout. Successful OS identity/rename operations are abstract atomic steps. |

Each model has positive invariants, seeded unsafe controls and explicit reachability witnesses. A witness is an expected counterexample to a negated terminal predicate, not a discovered product bug. Historical entries remain partial. Delivery launch failure after receipt publication remains unknown and preserves the receipt; no rollback success is inferred. Stop timeout leaves the child alive in its witness and never claims forced cleanup. Docker dependency timeout, arbitrary OS failures and descendant ownership are excluded, not asserted safe.

Source correspondence:

- `MemorySession.append/messages/finish/fail` map directly to Append/Snapshot/Finish/Fail. Constructor deep copy and snapshot isolation are also checked against actual Python behavior.
- `_SqlSpace.prepare/seal/transaction` map to Prepare/Seal/Transaction; InvalidMode captures both invalid mode and non-bool write before mutation. Deterministic contracts use a fake physical transaction to prove entered and failed-entry cases, without simulating SQL engine guarantees.
- `start` writes STARTING only after pidfd acquisition and before writing the launcher pipe. Publish/Release preserve that ordering; Observe requires exact identity headers. `stop` validates receipt/marker containment, process start/executable identity, and private regular marker, then waits for the acquired pidfd. AST guards preserve identity-before-effects and disallow kill/terminate calls in stop. Startup rollback is a separate owned-child path and is not conflated with stop.

The registry's source bindings and lint provide review drift detection. The formal result does not establish successful real Linux deployment or PID reuse integration on Windows. Those environment-specific checks remain the project's integration responsibility.
