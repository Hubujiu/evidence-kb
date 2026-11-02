# Database registry cleanup ownership

## Mature reference and confirmed defects

Before changing code, the existing `BlobService._dispose` and `BlobService.close`
were reviewed at baseline `8160c051`. They already reject a competing cleaner and
close admission before snapshotting owned routes. The database registry used the
same ownership shape but lacked those two protections:

1. While one thread was outside the lock in `adapter.close()`, another disposer
   could call the same adapter again. The first eventual route deletion could
   then raise `KeyError` after the other thread deleted it.
2. `DatabaseService.close()` snapshotted routes while leaving service admission
   active. Another thread could register an adapter outside that snapshot; close
   then marked the service closed without releasing the late owner.

Deterministic Event/Thread tests reproduced both without live databases or timing
sleeps. The fix adopts the existing BlobService pattern, not a new lifecycle layer:
`closing` rejects concurrent cleanup, and `draining` is published under the service
lock before the close snapshot. Failed cleanup retains its route and can be retried
explicitly; registration remains blocked. A close refused because of an active
lease preserves the prior behavior and does not revoke the lease.

## Source-to-model mapping

| TLA+ action/state | Python owner |
| --- | --- |
| `service`, `owned`, `phase` | `DatabaseService._state`, `_routes`, `_Route.phase` |
| `leased`, `Acquire`, `Release` | `DatabaseService.lease`, active-call increment/decrement under lock |
| `RegisterLate` | `DatabaseService.register` service-state admission |
| `BeginClose`, `snapshot` | service drain and route snapshot in `DatabaseService.close` |
| `DrainBusy` | `_dispose` marks route draining but refuses cleanup with an active lease |
| `BeginDispose`, `cleaners` | `_dispose` claims exclusive cleanup before releasing its lock; the cleaner count is a model observer, not a new runtime counter |
| `CleanupFails` | exception handler retains owned route in `cleanup_failed` |
| `CleanupCompletes` | normal adapter completion followed by route/grant removal |
| `FinishClose` | service publishes `closed` after its snapshot has been disposed |

`AllowDuplicateCleanup` removes the closing guard. `KeepAdmissionOpen` restores
the old admission state during close. Independent negative configurations isolate
`NoConcurrentCleanup` and `ClosedOwnsNothing` respectively and must exit 12 on the
named invariant, not any parse/runtime failure. The real production code has no
switch enabling either legacy behavior.

Source bindings include the database register/lease/dispose/close functions and
the BlobService reference. Fingerprints detect review drift, not refinement. The
new Python contract module checks the actual AST guard ordering and rejects both
seeded guard deletions. Behavioral tests additionally cover competing close calls,
active leases, retained cleanup failure and explicit successful retry.

## Finite scope and omissions

- Two distinct route identities; the second can register once. Each route has one
  bounded active lease, two competing cleaner candidates and at most one injected
  cleanup failure. One accepted service-close snapshot is modeled.
- Lock-held state updates are atomic. External adapter cleanup is split into claim
  and success/failure so overlapping callers are explicitly explored.
- Repeated service close after a cleanup failure is represented by renewed disposal
  of the same retained snapshot, not automatic retry in the implementation.
- No fairness/liveness is assumed for arbitrary adapter callbacks. Stable terminal
  states are intentional (`CHECK_DEADLOCK FALSE`), not evidence of progress.
- This is partial coverage of `storage_registry`: StorageService grants, dataset
  identity, generation/ABA binding, BoundDatabase prepare/seal, owner-lease callback
  behavior and full BlobService lifecycle are not proved by this model.
- No database or OS resource-disposal correctness, unbounded-thread safety or
  machine-checked Python-to-TLA refinement is claimed.

## Evidence and execution

`evidence/python-before.log` retains four failures from the old implementation;
`python-after.log` records all five new contracts passing. The focused database,
blob and generic-storage modules pass 43 tests. The positive TLC model exhausts
200 distinct states (390 generated); both independent old-behavior controls find
the intended invariant violation. Final formal-run metadata and logs are retained.
The stage is based separately on formal infrastructure commit `f306692`, not on
the transaction-knowledge patch. The parent owns full-project validation and PRs.

```sh
python -m unittest kb_platform.rag_testing.database_registry_lifecycle_contracts -v
python -m kb_platform.rag_testing.formal_state_machines \
  --output /tmp/new-database-registry-formal --jar "$TLA_TOOLS_JAR"
```

All faults and adapters are synthetic; other focused tests use temporary local
SQLite/files and synthetic transports. New remote provider/model/judge calls: zero.
These bounded findings and fixes do not imply that all storage states are bug-free.
