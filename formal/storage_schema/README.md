# Schema admission and initialization ownership

This partial finite audit covers the real structured prepare/seal owners and the
low-level bound handle's prepare lifecycle. It does **not** turn a handle's seal
into a database-global ban on another authorized initialization, implement a SQL
engine, or claim all storage state machines are correct.

## Mature implementation and contract decisions

[SQLAlchemy metadata documentation](https://docs.sqlalchemy.org/en/20/core/metadata.html#altering-database-objects-through-migrations)
distinguishes application metadata and database schema migration. Altering a
`Table` object is not an ALTER of an existing physical table.
[Alembic autogeneration](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
compares target metadata with the database and produces candidate migrations for
review, rather than treating matching Python object identity as migration proof.
The existing SQLAlchemy Core compiler, SQLite/SQL provider, migration locks and
content-hash checks remain in use; no migration library is copied or replaced.

The project explicitly rejects same-version content changes and supports retrying
initialization for the **same** schema. Successful initialization therefore owns
an immutable `(owner, version, provider digest)` identity. Metadata can be built
before prepare; modifying registered metadata after prepare is not a supported
live-schema update. In particular this patch does not promise safe concurrent
mutation during initialization/transactions or recompile every DDL on every
transaction. A subsequent prepare must nevertheless not falsely acknowledge a
changed identity as the previously prepared schema.

The original SQLAlchemy `Table` objects are retained. Deep-copying them would
break the direct structured adapter's identity-based expression validation.
The digest remains provider-declared and opaque to portable clients; matching
content semantics during discovery does not justify ignoring owner or version.
A provider that dishonestly reuses the same opaque revision for changed content
is outside the trusted-revision contract; this patch does not detect that claim.

## Fixes

- The endpoint used `self._package.digest == selected.digest` for retries. Both
  objects can be the same mutable metadata-backed schema, so both digests change
  together while the physical schema remains unchanged. Equal opaque digests
  could also incorrectly acknowledge another configured owner through this cached
  path. That did not replace the selected package or demonstrate cross-owner data
  access. The endpoint
  now captures the requested full identity and publishes it only after actual
  backend prepare returns. Its existing lock protects publication and retries.
  Neither a failed prepare nor lost response resets or invents server readiness.
- Portable `prepare` and `seal` previously returned success for any JSON response,
  including absent/UNKNOWN/malformed status. Each now requires exactly its defined
  PREPARED or SEALED acknowledgement. Invalid ACKs raise the existing schema
  protocol error. There is no automatic resend, compensation, rollback claim, or
  discard of a successfully installed server identity.

Direct `StructuredSqlSpace` already rejects wrong owners and persisted same-version
hash changes, publishes its package after provider preparation, and seals before
ordinary transaction entry. `BoundDatabase.for_consumer` preserves identity and
copies the source handle's seal snapshot; a pre-existing sibling is independent.
These paths are modeled and exercised without unnecessary production changes.

## Models and boundaries

| Configuration | Production correspondence | Finite claims |
| --- | --- | --- |
| `SchemaNegotiation` | endpoint prepare cache/close, backend return, client prepare/seal observations, first ordinary transaction | Four keys, two content digests, two owners, one endpoint, at most three prepare attempts. Successful fixed identity, no premature publication, closed admission rejection, exact ACK knowledge. |
| `StructuredLifecycle` | `StructuredSqlSpace.prepare/seal/transaction` and fresh binding to the same physical dataset | Three handles, two same-version schema contents. Required preparation before seal/use, owner/hash checks, independent handle seal, failure/retry and reopening. |
| `BoundLifecycle` | `BoundDatabase.prepare/seal/transaction/for_consumer` with existing service leases | Same finite bounds plus two space/dataset/registration tuples and a consumer-name match flag. Consumer clone preserves binding identity and inherits seal; a sibling/fresh authorized handle can still prepare. Bound SQL permits seal/ordinary transaction before schema installation, unlike structured SQL. |

`SchemaNegotiation.sealed` represents the effective initialization closure of the
underlying store (explicit seal or ordinary transaction), not an assertion that
`PortableStructuredEndpoint._sealed` is written by `begin`. The cached same-schema
ACK remains valid after effective closure; it does not call backend prepare again.
`pending` is a completed server result awaiting client observation, not an unlocked
half-executed endpoint call. Close may happen before that observation without
retroactively invalidating historical success. Backend failure before effect and
failure after durable effect but before publication are separate outcomes.

`Begin` deliberately requires unchanged configured metadata: using mutated metadata
inside active/new transactions is outside the supported contract, while the
adversarial re-prepare action is still checked. Database effects are abstracted as
one persisted identity. The model does not prove atomic DDL, dirty MySQL recovery,
multiple migration versions, backend honesty, SQL behavior, cross-model refinement,
or concurrent initialization of direct handles. The persisted `_database_migrations`
dirty/step protocol (including MySQL per-step commits and downgrade prevention) is
a separate unmodeled Python owner, not an external-database detail proved by the
abstract `physical` variable. Direct initialization is assumed
caller-serialized; endpoint calls use the actual RLock. `matchingConsumer` means
the optional consumer name is omitted or matches the grant; it does not compare
the runtime generation identity passed to `for_consumer`. That runtime identity
is not the preserved space/dataset/registration tuple. Initialization deliberately
does not require an active consumer lease. No liveness is claimed.

Each positive CFG has explicit safety assertions. Twenty-four named negative/witness
configs must produce TLC exit 12 for their named invariant, not timeout, parse
failure or incomplete-state errors. Separate mutable-identity and foreign-owner
controls distinguish the two cached identity errors. Prepare and seal ACK mutants
are separate. A seal must leave every existing sibling unchanged, not merely
leave unbound handles open. Clone-identity and wrong-consumer mutants are independent. Reachability witnesses require useful retry after seal/lost ACK,
failure recovery, transaction admission and sibling preparation, so disabling
all initialization cannot masquerade as success.

## Executable contracts and guardrails

Fourteen SQLite/JSON contracts cover changed metadata with unchanged physical hash,
foreign opaque digest owner isolation, unchanged equivalent schema retries,
original Table identity, lost ACK without backend replay, real failed SQLite DDL
and repair, synthetic provider failure after actual durable preparation, malformed
prepare/seal ACKs, closed/unprepared admission, restart with persisted rows/hash,
first-transaction closure, lost seal ACK and bound consumer/sibling identity/seal
semantics (including a previously created sibling after its parent seals).
Synthetic transport/provider failures are labeled; no production incident is
claimed. Existing portable foreign-revision and service contracts still run.

Mandatory AST lint checks initialization, the full requested identity, cached
comparison, successful-return publication order and both exact ACK checks. Ten
source mutants remain rejected even after recalculating their AST source hashes.
Source hashes are drift detectors, not semantic proofs. Existing handle/commit
bindings are refreshed only for the reviewed admission changes; those models and
contracts remain part of the required checks.

Raw local logs, traces and failure snapshots remain private and are not required
inputs to any test. Published content is production code, normal implementation
documentation, necessary contracts/lint, finite TLA/CFG and registry bindings.
Run both mandatory commands with new output directories:

```sh
uv run --no-sync python scripts/check_project.py --output .local/checks/new-ordinary
uv run --no-sync python scripts/check_project.py --formal-only --output .local/checks/new-formal
```

Only CI of the exact published tree can establish that tree's check result;
focused checks or a different local tree must not be described as a full pass.
No paid model/embedding/judge call, main merge or deployment is part of this work.
