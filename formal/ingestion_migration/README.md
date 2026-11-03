# Ingestion and migration commit boundaries — partial, model-only

This batch found **no new confirmed production defect**. It adds three bounded
models, executable regression contracts and mandatory structural lint around
existing safeguards. It does not change production ingestion, transfer or reset
semantics. Seeded unsafe variants below are test controls, not claims that those
variants occur in the current source.

## Mature references consulted before modeling

- [Apache Iceberg reliability](https://iceberg.apache.org/docs/latest/reliability/):
  prepare immutable data separately, publish an atomic metadata reference, and
  revalidate an operation's assumptions against current state before committing.
  This motivates distinguishing original-object storage from ingestion metadata,
  and new-space initialization from reset configuration publication. We do not
  introduce automatic retry of parsing, model requests or administrative changes.
- [SQLite isolation](https://www.sqlite.org/isolation.html): separate readers see
  committed transactions, while an immediate transaction excludes other writers.
  This is the backend assumption behind the repository's existing
  `database_migrations.migration_connection`; the TLA model does not prove SQLite.
- [SQLite snapshot/backup guidance](https://www.sqlite.org/backup.html): a coherent
  database snapshot is not an arbitrary copy of a changing database file. Existing
  `read_sql_snapshot` uses a transaction and explicit frozen-source precondition.
  No raw live database copy is added here.

## Source mapping and exact scope

| Boundary | Existing source anchors | Checked behavior |
| --- | --- | --- |
| Parse → ingest metadata → build admission | `documents.py:_Bundle.missing`, `parse_native_document`; `core.py:Platform.ingest`, parse gate in `Platform.stage` | READY/PARTIAL/BLOCKED/**FAILED** are distinct; original hash/version and current source revision must match before metadata commits; only READY enters build creation. |
| Knowledge import | `knowledge_transfer.py:validate_export`, `import_knowledge`; `storage_adapters/structured_transfer.py:_identity`, `import_structured_snapshot`; `database_migrations.py:migration_connection` | A new, explicitly offline target is identity checked. Domain rows and receipt share one transaction; imported publication is cleared. Source records are copied, not mutated. |
| Business reset | `storage_reset.py:_validate`, `plan_business_reset`, `initialize_business_reset`, `reset_to_file` | New business spaces are initialized before a flushed temporary configuration is exclusively linked into place. A competing output is never overwritten; bindings classified as protected ledger/identity remain unchanged. |

`Platform.stage` is a shared source anchor: this batch reviews its **parse admission
prefix only**. Authority publication, subsequent staging/search generation and
embedding jobs belong to the separate PR #43 models, not this model. Budget-ledger
`persistence_migration.import_ledger` sealing/recovery belongs to the budget batch.
The registry now explicitly includes the previously omitted reset and structured
transfer owners; adding their hashes is review-drift tracking, not a proof.

## Models and controls

### ParseAdmission

One in-flight ingestion, one original identity, an explicit nonempty-evidence flag, four parse outcomes, an optional
wrong parser identity, and at most one concurrent policy revision/revocation.
Store, parse, commit, reject, build and precommit crash are distinct transitions.
Assertions check READY-only build admission, original identity and current policy
at commit. Build always retains its independent nonempty-evidence guard; removing only READY admission must therefore expose a PARTIAL parse with retained evidence, not an empty FAILED/BLOCKED parse. Seeded controls independently remove each of these three guards.
The abstraction ends at admission; it does not assert that every future authority
change can be ignored or model the eventual validity of a published search index.

### KnowledgeTransfer

Two importers, one package and one target. The transaction owner serializes read,
local row insertion, local receipt insertion and publication of both. Crashes
before commit discard transaction-local work. A second invocation can reach exact
idempotent reuse; a wrong requested dataset is rejected. Controls expose domain
rows before the receipt, or retain the source publication pointer. Invariants
cover atomic visibility, reset publication, immutable dataset identity and at most
one fresh import. Exact row-content comparison is represented by the single
unchanged package/target abstraction, and independently tested against real SQL.

### StorageReset

Two producers, two business roles and two protected roles, one destination.
Initialization may stop partway; temporary-write and flush precede publication.
An unsuccessful producer may leave unused new spaces, but no invalid output is
published and neither old business data nor protected bindings are erased.
Controls allow premature publication, overwrite a winner, or reset protected
identities. Symbolic `(producer, role)` new spaces stand for fresh UUID-backed
identities; arbitrary provider allocation failures are not modeled internally.

Each fixed model also has a `Success` configuration checking the deliberately
false invariant `SuccessUnreachable`. TLC must reach the actual build, commit plus
idempotent repeat, or reset publication and violate that named invariant (exit 12).
These three existential success witnesses prevent an all-reject/no-progress model
from being mistaken for useful coverage. They are **not** bug counterexamples or
eventual-progress/liveness proofs. Positive configurations exhaust their finite
state spaces; no fairness or unbounded-completeness claim is made.

## Real contracts and mandatory lint

`ingestion_migration_contracts.py` adds five ordinary discovered contracts:

1. Actual native TXT/PNG/HTML parsing exercises all four outcomes, real SQLite ingestion
   and a successful lexical build. Incomplete parses cannot call search staging;
   original bytes/hash survive and another tenant cannot build the scope. A real HTML paragraph plus an unfetched image supplies nonempty PARTIAL evidence; the test verifies usable units without committing a build.
2. A deterministic source-policy change inside parsing rejects stale metadata.
   The original-store write is observed and subsequently read **without rewriting
   it**, while the old binding/version remains intact. An unreferenced original
   after rejection is permitted, not mislabeled as a committed document.
3. A fault immediately after the actual SQL receipt INSERT but before transaction
   commit rolls back both receipt and domain data. A real retry succeeds once,
   exact repeat is idempotent, publication remains empty and UNKNOWN jobs survive.
4. Two complete real reset operations are interleaved at exclusive publication.
   Exactly one complete configuration wins; the losing link cannot replace it,
   and original data plus ledger/identity bindings remain readable and unchanged.
   This is a deterministic single-process interleaving, not a multiprocess claim.
5. Ten source mutations refresh **all** relevant review hashes before invoking
   actual registry lint. READY, original identity, stale revision, publication
   reset and its pre-import ordering, nonempty evidence, atomic receipt placement, dataset identity, exclusive link and protected
   role selection violations are still rejected.

Existing knowledge-transfer and reset contracts remain unchanged and run alongside
these tests (18 cases total). Fixtures use only temporary synthetic data, native
parsers, SQLite and lexical search. They construct no paid OCR/Embedding/model
execution and do not represent production RAG quality results.

`formal_state_machines.registry_violations` invokes `ingestion_migration_rules` as
a mandatory gate. This is focused AST structure/order lint, not a complete Python
semantic analyzer. Hash refresh alone cannot bless the listed unsafe variants;
unlisted transformations still require source review and real regression tests.

## Reproduction and evidence honesty

Run from the repository root with the existing development environment:

```sh
uv run --no-sync python -m unittest -v \
  kb_platform.rag_testing.ingestion_migration_contracts \
  kb_platform.rag_testing.knowledge_transfer_contracts \
  kb_platform.rag_testing.storage_reset_contracts
uv run --no-sync python scripts/check_project.py --formal-only
uv run --no-sync python scripts/check_project.py
```

The formal command requires the separately prepared pinned TLC and Java 21; it
does not replace ordinary checks. Final commit-bound outputs are kept separately
from development runs, and a failed ordinary gate is never renamed a formal PASS.

Development evidence is retained locally, not overwritten: the first new contract
run had five fixture errors because SQL was sent through the named business-query
facade; using its legitimate structured capability fixed the fixture. A first TLC
run rejected an incomplete next-state assignment caused by missing parentheses in
the new transfer model; that model-authoring error was corrected. Neither failure
is presented as a product bug. Baseline production behavior passed the corrected
four real regression contracts before model/lint authoring.

## Remaining obligations

- OCR and page-ensemble recovery, extraction quality and arbitrary documents remain
  source-bound inventory rather than a complete page-level state-machine model.
- Original-object correctness, operator-enforced source-frozen/target-offline
  conditions, SQL/provider transaction guarantees and filesystem locking are
  assumptions. No online/distributed migration protocol is claimed.
- Process failure is modeled; power-loss durability, including directory fsync,
  remote filesystems, malicious mutation and unbounded rows/concurrency are not.
- Object transfer, budget migration, and composition with authority/search models
  remain separate work. `ingestion_migration` intentionally stays **partial**.
