# Persistent journal and document revision owners

These are two repo-owned protocols, not SQL engine models and not the schema
prepare acknowledgement model or storage-call admission model.

## Mature primitives retained

The code continues to use SQLAlchemy transactions, SQLite `BEGIN IMMEDIATE`,
PostgreSQL transaction-scoped advisory locks and MySQL session-scoped migration
locks. [Alembic's dialect-specific DDL implementations](https://alembic.sqlalchemy.org/en/latest/api/ddl.html)
provide the mature distinction between transactional and nontransactional DDL;
this work does not introduce a second migration framework or automatic dirty
recovery. SQLite explicitly starts its transaction rather than relying on the
legacy driver's implicit begin behavior.

[SQLAlchemy's version counter documentation](https://docs.sqlalchemy.org/en/20/orm/versioning.html)
explains the established compare-the-previous-version and require-one-matched-row
pattern. The existing Core SQL document store already follows that pattern, with
write serialization, persistent tombstones and a bounded integer revision. It is
not replaced by a new home-grown transaction engine or ORM. No new document-CAS
production defect is claimed by adding its previously missing model.

## MigrationJournal

Two versions, two statements per version and two prepare attempts model the
journal's `(version, hash, dirty, step)` and durable statement effects separately
from in-flight transaction changes. SQLite/PostgreSQL roll back the whole
prepare transaction. MySQL commits dirty first, each completed step, then clean;
a DDL effect may be durable before its corresponding step record. A failure at
any represented boundary leaves either a rolled-back transaction or a durable
journal which the next attempt must validate. Changed hash, dirty and downgrade
are distinct refusals. Runtime grants happen after final commit and connection
cleanup. A grant failure does not undo migrations or authorize DDL replay.

Identity initialization distinguishes persisted identity, absent identity and
successful in-memory publication. Expected existing identity is not permission
to create a missing dataset; loss of a commit acknowledgement does not publish
success. Identity-table DDL, random UUID generation and wrong physical routing
are abstracted; existing actual identity contracts remain necessary.

The cleanup fix ensures an error from MySQL lock release or its commit cannot
skip `close()`. Because the migration engine uses a pool and `GET_LOCK` belongs
to a physical session, that error first invalidates the connection rather than
returning an unconfirmed lock-owning session to the reusable pool. The exception
is still propagated, including when migrations were already durable. Close is
attempted even if invalidation raises. The earliest body/commit failure remains
the propagated exception; rollback, release, invalidation and close failures add
exception-type-only notes rather than replacing it. With no earlier failure, the
release or close error propagates normally. This uses SQLAlchemy
[connection invalidation](https://docs.sqlalchemy.org/en/20/core/connections.html#sqlalchemy.engine.Connection.invalidate),
not a pool-wide reset or migration retry. Neither model nor fix promises that a
failing driver or network proves server-side release immediately.

## DocumentRevisions

Both an empty source and a source containing a live row and tombstone are initial
possibilities. Two keys and two writers perform up to three serialized operations over an
abstract maximum revision of three. A caller may supply none, create-only zero,
a stale/current version, or MAX. A pending caller expectation survives another
writer's delete/recreate. Reads of the row and the actual write are one atomic
transaction under the existing serialization primitives. Deletes preserve a
higher tombstone revision; absent/tombstone delete returns false even with an
expected revision. Recreating a tombstone is allowed by expected zero, never by
an old positive revision. Exhausted live records and tombstones cannot wrap.

Export takes a quiescent cut and the finite source then stops writing. Import
compares an empty, exactly equal, or different target, including tombstone
history; it inserts atomically or refuses without modifying that target. Source
and target dataset checks and validated immutable snapshot content are boundary
assumptions, not a proof of JSON/hash/SQL internals. The model does not claim
arbitrary concurrent clone/export refinement. Existing real SQLite concurrent
CAS, create, delete/recreate, snapshot mutation and import rollback contracts
remain required; the new MAX contract uses the real 64-bit boundary.

## Negative controls versus witnesses

`AcceptDirty`, `AcceptHash`, `AcceptDowngrade`, `MissingDirty`, `EarlyClean`,
`EarlyGrant`, `EarlyIdentity`, `ReleaseLeak`, `ReleaseReuse` and `MaskPrimaryError` inject faults and
must violate their named safety invariant. Only the release cleanup defects are
newly reproduced implementation bugs. Other controls are synthetic sensitivity
checks, not allegations that those bugs exist in the source.

`RevisionABA`, `IgnoreExpected`, `RevisionWrap`, `LoseTombstones` and
`OverwriteCloneTarget` are synthetic document-protocol fault controls.

All `*Witness` configurations instead run the correct switches and negate a
specific reachable success/refusal observation: upgrade, dirty refusal, durable
retry, lost identity acknowledgement, recreate, stale rejection, exhaustion or
idempotent clone. Their expected invariant violations are reachability evidence,
not production failures or fairness/liveness proofs.

## Engineering checks and limits

`persistent_storage_contracts` adds real SQLite journal/commit/grant and revision
checks plus deterministic cleanup failures. The existing controlled storage
integration stage calls the same journal scenario against disposable SQLite,
PostgreSQL and MySQL resources. Its MySQL release test injects a release error in
a healthy actual connection and verifies a different physical connection can
acquire the same lock; reusing the original pooled session would conceal a leak.
No production database or paid model is involved.

GET_LOCK acquisition/result-confirmation failure remains a driver boundary. The
locked SQLAlchemy 2.1.6 implementation routes DBAPI fetch exceptions through its
connection error handler; this model assumes disconnect/cancellation handling
invalidates an unconfirmed physical session as appropriate. It does not prove
that an arbitrary interruption between server acquisition and local confirmation
is safe, and no unverified acquisition defect is asserted by this stage.

`persistent_storage_lint_contracts` guards unconditional close, session
invalidation and propagation even after the source digest is refreshed. It is
mandatory through normal nonzero unittest discovery. Both ordinary
`check_project.py` and its additional `--formal-only` entry remain required.
Driver atomicity, SQL correctness, lock fairness, arbitrary migration statements,
external database writers/corruption, unbounded histories and cross-model
refinement are not proved by these finite models. Raw execution artifacts remain
in private `.local` outputs, not in source control.
