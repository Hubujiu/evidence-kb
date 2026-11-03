# File receipt transitions: bounded concurrency model

This is a finite **safety** model of file-backed `RequestReceipts`, not a proof of the complete provider, budget, filesystem or SQL implementation. It does not establish provider behavior, RAG quality, power-loss durability or eventual recovery.

## Mature synchronization precedent and selected scope

Before implementation, the existing `StructuredSqlSpace.guard` was compared with these primary references:

- [SQLite locking and concurrency](https://www.sqlite.org/lockingv3.html) describes coordination across both processes and threads. [SQLite transactions](https://www.sqlite.org/lang_transaction.html) defines `BEGIN IMMEDIATE` and contention failure. The existing SQLite guard already used this mechanism, which is now extracted into `database_providers/sqlite_guard.py` and reused without changing its acquisition, timeout or error mapping.
- [SQLite corruption guidance](https://www.sqlite.org/howtocorrupt.html#_file_locking_problems) explains the requirements for working filesystem locks, why a live database must not be unlinked/replaced, and why SQLite connections must not cross `fork`. The helper opens and closes a fresh connection inside each operation. There is no stale-lock deletion, expiry or PID heuristic.
- [filelock documentation](https://py-filelock.readthedocs.io/en/stable/) documents native cross-process locks and platform fallback. It was considered, but is not an installed core dependency here; the existing SQLite primitive avoids introducing a dependency or a marker-lock fallback. No external source code was copied.

The new sidecars contain **no receipt state, schema, spend ledger or reservation**. They exist solely as stable lock identities. `RequestReceipts` construction does not initialize a storage space, native plugin, connection pool or owned long-lived connection. Existing `.json`, `.pending` and `.terminal` paths and content remain authoritative. HTTP execution is outside the guard. Different logical IDs have independent guards.

## Concrete failure and repair

Legacy `finish` read STARTED without serializing the subsequent physical write, terminal publication and pending removal. Another `finish` could settle the same identity and a linked `begin` could create the next pending marker. The stale finisher then overwrote the old receipt and removed the **new** pending marker, allowing another physical attempt while the previous attempt remained unresolved. A competing `reconcile` could similarly have its terminal result overwritten by a stale UNKNOWN finisher.

All file-mode `begin`, `finish` and `reconcile` read/modify/publish/remove sequences now run under the same logical-ID SQLite guard. `finish` additionally rejects an absent or mismatched pending physical ID, matching the database-backed transition's ownership condition. Database-mode receipts are unchanged.

A crash releases only the OS/SQLite lock, not an unresolved receipt. In particular, a crash after physical COMPLETE but before pending unlink leaves a blocked marker. It is **not** automatically deleted or resent. That interrupted publication can require operator inspection; this change intentionally does not add an automatic repair/migration or claim recovery liveness. Existing UNKNOWN data and all budget records are retained.

## Actions and source binding

| Model | Source boundary |
| --- | --- |
| `StartBegin`, `BeginClaim` | `RequestReceipts.begin`: allocate identity, acquire guard, exclusive pending creation |
| `BeginPersist`, `BeginReturn` | STARTED physical JSON write, return physical identity before external send |
| `StartTransition`, `Read` | `finish` / `reconcile`: guard acquisition then current physical and pending ownership validation |
| `Write` | `_write`: atomic replacement of the physical JSON receipt |
| `Publish` | `_write_path` of the logical `.terminal` marker |
| `Unlink`, `Release` | remove current `.pending`, then close guard connection |
| `Crash` | process death at any split boundary; release volatile ownership, preserve completed file operations |
| `settled` | specification-only first terminal result, used to detect later overwrite |

The model bounds one logical ID, two concurrent actors and three allocated physical identities. Begin rejection consumes an allocated identity, as UUID allocation precedes admission in the source. Retry metadata is abstracted as a valid link to the current terminal marker, including explicit format correction after COMPLETE. Arbitrary invalid input, UUID collisions, non-cooperating/old-version writers and multiple SQLite library copies are excluded. The model splits file steps rather than pretending the entire JSON update is a transaction. Each completed file replacement is assumed atomic. Working local filesystem locking and process-death lock release are assumptions, also exercised by the controlled process tests.

There is no fairness or liveness claim. Persistent UNKNOWN or an interrupted publication may remain blocked indefinitely. The model does not simulate torn writes, lost directory entries, storage/controller failure, filesystem lock bugs or power loss; existing file fsync does not establish directory fsync durability. Use database-backed receipts when stronger transactional publication is required. Mixing old unguarded writers with new guarded writers is not safe; stop old writers before switching versions.

## Checked invariants and evidence

- `TypeOK`: bounded IDs, status and ownership domains.
- `SingleUnresolved`: at most one admitted STARTED/UNKNOWN physical identity.
- `PendingCoversUnresolved`: each admitted unresolved identity still owns pending authority.
- `TerminalImmutable`: a physical terminal result is not overwritten.

Pinned `tla2tools` 1.7.4 (SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`), Java 21, two workers:

- `FileReceipts.cfg`, `Safe=TRUE`: **1,014,219 generated / 107,349 distinct states**, depth **35**, no invariant violation.
- `FileReceiptsLegacy.cfg`, `Safe=FALSE`: `TerminalImmutable` fails at depth **11**. A finisher reads STARTED, reconciliation writes COMPLETE, then the stale finisher overwrites UNKNOWN.
- `FileReceiptsOverlap.cfg`, `Safe=FALSE`: `SingleUnresolved` fails at depth **18**. A stale UNKNOWN write overlaps a new admitted STARTED identity. The separate deterministic Python baseline reproduction also demonstrated removal of the next attempt's pending marker and admission of a third identity.

Only a TLC invariant counterexample is accepted as negative evidence; parse failure, timeout or missing tools is not a successful control.

```sh
java -XX:+UseParallelGC -Xmx1g -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 2 -cleanup -config FileReceipts.cfg formal/file_receipts/FileReceipts.tla
java -XX:+UseParallelGC -Xmx1g -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 2 -cleanup -config FileReceiptsLegacy.cfg formal/file_receipts/FileReceipts.tla
java -XX:+UseParallelGC -Xmx1g -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 2 -cleanup -config FileReceiptsOverlap.cfg formal/file_receipts/FileReceipts.tla
```

The unified offline contracts cover two-client admission, finish/reconcile races, matching pending ownership, UNKNOWN/restart, partial publication and per-logical guard use. The existing foundation integration entrypoint runs controlled cross-process lock exclusion and process death before physical publication, after physical publication, after terminal publication and after UNKNOWN. Those subprocess tests do not run in the offline contract worker, whose process prohibition is unchanged. Fixtures are synthetic and all provider, embedding and judge call counts are zero.

`receipt_transition_checks.py` checks that the exact logical guard dominates receipt reads/writes/removal and that pending-owner rejection precedes publication. It also checks the extracted primitive's `BEGIN IMMEDIATE` acquisition, connection release and stable lock identity. Unified lint contracts include parseable guard/identity/publication/lock mutations and legal annotation/spacing controls. These are source-shape drift checks, not an equivalence proof.

The canonical `formal/registry-v1.json` preserves the database receipt and remote transport inventory as remaining obligations. Its mandatory source lint executes both guard checks before TLC; refreshed source hashes do not bypass removed or weakened guards. `scripts/check_project.py --formal-only` runs the positive model and both named invariant-negative controls.
