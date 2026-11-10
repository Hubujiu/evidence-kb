# Storage shutdown composition

`StorageShutdown` is a bounded safety abstraction of ApplicationStorage draining
operations and retaining cleanup owners, composed with Redis provider cleanup.
Two provider slots and one endpoint are modeled. There are at most two active
operations and two physical close attempts per provider. The first physical
attempt may fail; the second may succeed. A failed endpoint close stutters
because the endpoint remains owned; its successful close removes the slot.

Source mapping:

| Model | Source |
| --- | --- |
| Admit / Leave / BeginClose | `ApplicationStorage._operation` and `close` |
| CloseEndpoint | endpoint close-before-delete in `ApplicationStorage.close` |
| StartPhysical / PhysicalFail / PhysicalSuccess | `RedisCacheProvider.close` |
| PopSuccessful | provider close-before-pop in `ApplicationStorage.close` |
| Reenter | injected omission of Redis active-depth refusal |
| Finish | no remaining cleanup owners |

Normal provider return promises physical cleanup succeeded. The consumer removes
an owner only after that return. This shared event is the composition boundary;
two independent local PASS results would not establish it. `AcknowledgedCleanup`
checks the provider promise and `RetainedUntilClean` checks the resulting consumer
invariant. `NoCleanupDuringOperation` and `NoReentrantCleanup` check admission and
active-close exclusion. `CompleteOwnsNothing` requires real completed effects.

The repaired Redis owner seals admission separately from acknowledging cleanup.
Its RLock spans connection use or physical close; `_active` rejects same-thread
reentrant cleanup as well as recording nested connection ownership. Failure
leaves admission sealed and cleanup unacknowledged, so an explicit retry calls
the client again. Model lock ownership is implicit in the enabled actions;
provider connection nesting is covered by runtime and source contracts, not a
full thread scheduler refinement.

The previous early `_closed` assignment is reproduced by EarlyAck: provider
acknowledgement precedes physical success. EarlyPop injects consumer removal
before cleanup; Reentrant removes active cleanup exclusion. Each must fail its
exact named safety invariant (TLC exit 12). ReachRetry is a reachability witness
for both providers failing then succeeding and the application completing;
ReachBusyDrain witnesses close admission with two active operations still owned.
They are deliberately negated reachability predicates, not seeded defects.

`storage_shutdown_lint.py` guards the reviewed owner ASTs, including effect order
and finally-balanced ownership. Mutation contracts reject unsafe shapes without
depending on source hashes. Runtime contracts use the actual Redis provider
with a mocked physical client and the actual ApplicationStorage close method;
they exercise failure, retry, endpoint ordering, busy admission and reentry.

Assumptions: provider normal return truthfully reports completed cleanup; Python
RLock excludes other threads and finally runs for ordinary Python exceptions;
the application owns its cleanup collections. The latter guarantee is checked
for Redis here; arbitrary third-party implementations still need their own
contract evidence. Registry cleanup and other provider types are abstracted by
their successful-return contract, not proven by this model. Abrupt process
termination, Redis network recovery, arbitrary resource counts, unbounded
fairness and eventual close are excluded. A stalled or repeatedly failing client
can prevent close; no liveness claim is made.

Targeted initial TLC evidence is in the fresh Linux run directory
`/opt/evidence-runs/formal-composition-20261103/storage-tlc-01`. Both positive
configs exited 0; three fault controls and two reachability witnesses exited 12
with the expected invariant. These are bounded model results, not a source
refinement theorem or production availability measurement.
