# Acceptance binding and client output workflows (partial)

## Mature protocol references and scope

[SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) separates a
commit decision from the individual file writes it coordinates. A JSON rename
is atomic for one name, not a transaction spanning every split file.
[Python `os.replace`](https://docs.python.org/3/library/os.html#os.replace)
documents successful replacement atomicity and filesystem restrictions; it does
not establish power-loss durability for this application's directory entries.
[PostgreSQL WAL](https://www.postgresql.org/docs/current/wal-intro.html) illustrates
the established write-ahead ordering principle. Here the existing STARTED receipt
precedes a remote effect, and immutable per-turn request/response files plus the
physical ledger preserve observations. This is **not** a new database, WAL redo
implementation, queue, automatic recovery or retry mechanism.

The immutable dependency snapshot is documented in
[formal-acceptance-dependencies](../../docs/formal-acceptance-dependencies.md).
PR #34's current capture settlement predicate remains intact. PR #46 models
exclusive preparation ownership for one operation; PR #34 models physical
settlement. Neither previously modeled these multi-file client workflows.
Server session writer authority is also a different owner.

## Confirmed implementation defect and narrow repair

The binding code already rejected differing existing split artifacts, but wrote
`scenario-inputs.json` unconditionally. A real-file contract interrupts publication
immediately before `prepared.json`, retaining two splits and the scenario file.
Changing the fixture scenario before explicit re-entry then overwrote the earlier
scenario rather than rejecting the differing artifact. This is a publication
identity defect, not a duplicate model call or an authority-revocation finding.

`_publish_bound_json` now owns the same exact-value guard for split, scenario and
descriptor JSON: differing existing content rejects; matching content is read-only;
new content uses the existing unique-temp/fsync/replace helper. Existing operation
receipt checks and the preparation SQLite guard are unchanged. An interrupted
publication does not gain permission to repeat remote operations. No earlier
source artifact is rewritten during rebind.

An atomic `prepared.json` with checked hashes is itself a readable prepared
snapshot; `load_prepared` intentionally does not require the subsequent workflow
COMPLETE marker. Thus a failure after descriptor publication but before the final
marker can leave a usable coherent snapshot. The model asserts COMPLETE implies
all artifacts, not the reverse. No new resume API is introduced.

## BindingPublication model

Two splits have four artifact slots each plus one scenario file. The model checks
receipt eligibility, all initial reads, split pre/bind/post boundaries, each file,
final authority reads, scenario, descriptor and final marker separately. One
monotonic authority change, one scenario change, one interruption and one explicit
re-entry are available. COMPLETE, UNKNOWN and mismatching receipt snapshots are
separate starting cases; incomplete/error receipts map to the rejecting UNKNOWN
abstraction. Receipts are immutable throughout this read-only binding phase.

`ImmutableArtifacts`, `DescriptorHasAllFiles`, `ObservedAuthorityAgrees`,
`CompleteHasDescriptor` and `ReceiptReuseSafe` are checked with `TypeOK`.
`NoNewRemoteEffects` expresses this model's read-only scope; actual contracts also
verify zero mutating HTTP methods and exact reuse of six operation receipts.
Authority is compared between actual observations; a later authority change is
allowed, including between the last observation and publication. This is not an
instantaneous revocation or distributed transaction guarantee. The scalar epoch
abstracts a frozen authority revision with fixed scope and manifest identities;
real input contracts separately exercise those identities.

The `Overwrite` control changes only the scenario guard and corresponds to the
confirmed implementation defect. `NoFinalRead` is a seeded omission, not an
additional demonstrated bug. The COMPLETE and interrupted-partial-file witness
controls negate reachable predicates deliberately; they must produce named
invariant violations with nonempty traces, not parse errors or timeouts.

## AcceptanceExecution model

Two cases each have two user turns and one abstract physical request per turn.
The stages distinguish case admission, session, immutable request, STARTED,
send, response, capture binding, trace, three report formats, manifest and run
COMPLETE. One unknown outcome, caught failure or interruption is permitted.
The physical request facts survive client artifact failure. Published trace counts
include prior turns, and UNKNOWN blocks subsequent admission without resending.
A settled post-response exception can allow the next case; a blanket stop after
all exceptions would misstate the existing client contract.

`MarkerBeforeSend`, `PendingBlocksSend`, `TraceRetainsAllCalls`,
`CaptureRequiresAllTurns`, `CompletePublication` and `FixedDenominator` are checked
with `TypeOK`. COMPLETE means report artifacts were produced, **not** that all
cases passed. ERROR and NOT_RUN remain in the fixed denominator. Re-entry into
an occupied run output is rejection, not recovery. A process interruption can
leave RUNNING and physical evidence without a report; this is not fake COMPLETE.

Pending admission, dropped prior-call counts, early COMPLETE and dropped NOT_RUN
rows are independent seeded negative controls, not historical defect claims.
The success witness requires both cases' two turns; the blocked witness requires
one known first-turn usage, UNKNOWN on the second, two retained physical calls,
and a NOT_RUN next case in the completed report. Token vectors, multiple physical
retries per turn and provider settlement internals are delegated to the physical
ledger models, not asserted by this count abstraction.

## Source contracts, mandatory lint and limitations

`acceptance_output_contracts.py` uses real files, typed Suite/Plan/Trace inputs,
production binding publication, `DeliveryHTTPAdapter`, `execute_prepared`, runner,
scorer and report/manifest writers. HTTP is a synchronous `httpx.MockTransport`;
read-only canonical binding is a supplied typed fixture, not the real evidence
gateway. No socket, ASGI event loop, server or live provider is used here. Existing
controlled loopback preparation/process integration remains mandatory unchanged.
The fixture is synthetic engineering data, not production RAG acceptance.

Contracts cover the eight authority read boundaries, mismatching split/scenario
files, descriptor interruption, exact receipt reuse and rebind mismatch rejection,
source immutability, two cases/two turns, actual capture identities, usage totals,
UNKNOWN and post-response failures, fixed denominator, report/manifest failures,
hash verification and output reuse rejection. The new mandatory AST/order guard
runs in ordinary architecture checking and formal registry lint. Nine seeded
source mutants refresh source hashes before calling the real registry lint,
including scenario overwrite, authority omission, accounting loss and premature
report completion. This is structural defense, not general Python refinement.

All raw local execution outputs stay private. Only code, fixtures, TLA+/CFG,
registry bindings and implementation/verification explanations are published.
Initial model authoring parse and receipt-reentry mistakes are not product bugs
or successful validation; only frozen final-tree checks establish validation.
Finite exploration does not prove arbitrary size, liveness, malicious edits,
remote filesystem locking, power-loss durability, or full cross-owner refinement.
No model/embedding/judge calls, permission relaxation, retry or deployment occur.
