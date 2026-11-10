# Authority publication and standalone manifest postflight

Baseline: `c8ef7582f226c07f1c06d3874dba159708ca7fad` (production publication
code unchanged from `8160c051`). This is a **partial, finite safety abstraction**,
not a source refinement proof or a guarantee that the system has no bugs.

## Confirmed defect and public boundary

`SemanticPageService.current_manifest` authorized a snapshot before reading a
separate manifest store and implementation dependencies, then returned without
checking the authority again. The real `SemanticClient -> ServiceEndpoint ->
SemanticEndpoint` path adds no postflight check. A source ACL revision, canonical
manifest replacement, or member revocation during those reads could therefore
return stale standalone manifest metadata. The fix uses the existing
`EvidenceGateway._unchanged` pattern after the reads. `query` already has later
authority guards: this finding is **not** a demonstrated query-evidence leak.
It establishes authorization at the final check, not instantaneous revocation or
authorization at network response delivery.

Semantic dependency fingerprints include `semantic_pages_v2.py`; existing
semantic manifests therefore require explicit rebuild/import after this change.
This stage performs no rebuild or new embedding call and does not silently
accept old dependency hashes.

The synthetic public-contract regression uses the actual SQLite authority and
both durable-store and local-file manifest branches. The durable-store read is
a controlled callback; this is not an execution of PostgreSQL or its transport. Six changed-authority cases
fail before the fix and pass afterward; unchanged manifests still succeed.
`PublicationEmbeddingContracts.test_manifest_postflight_removal_is_a_source_lint_mutant`
removes this exact guard in a temporary source copy and requires review-drift
lint to reject it. Refreshing an anchor is not proof of behavior; the actual
public-contract regression remains independently mandatory.

## Mature references inspected before implementation

- [Pinned DSH/Cordis fiber](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/fiber.ts):
  the `_reload` epoch and effective-config publication guards keep superseded
  work from publishing as current. This informs snapshot fencing, not API
  equivalence to that runtime.
- [PostgreSQL 18 transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html):
  row locks and conditional updates must be understood at the transaction/row
  boundary, rather than treating separate reads as one global snapshot.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): one committed
  transaction is atomic subject to its storage assumptions. It does not make
  a search store and the authority database a distributed transaction.

Primary pages were read on 2026-11-02; no upstream runtime or model was executed.

## Mapping and bounds

| Model | Concrete owner / boundary |
| --- | --- |
| `Stage`, `snapshot`, `expected` | `Platform.stage`, frozen authority/KB revisions and expected relational manifest |
| `Store`, `Validate` | separate immutable search staging, then build validation; legacy staging may validate a now-stale build |
| `Publish` | `Platform.publish`; authority lock, expected pointer/revision checks, `MANIFEST_UPDATE` + `BUILD_PUBLISH` in one transaction |
| `Revoke` | membership/source-policy changes bump authority; already stored generations are not erased |
| `BeginRead`, `LoadManifest`, `ReturnManifest` | `SemanticPageService.current_manifest` preflight, external reads, final `_unchanged` |
| `publicationSafe`, `responseSafe` | ghost monitors recording validity **at publication/postflight**, not requiring an old response to remain current after later revocation |

Two builds, three authority values (`0..2`), one read, finite phases. KB and
tenant revisions are combined into a change identity; hashes/source/dependency
identities are assumed collision-free and stable during one checked operation.
No fairness or eventual progress is assumed. Outages/rollback can stutter;
successful commit is modeled atomically. Crash durability and lost commit
acknowledgments are delegated to the database/storage owners, not proved here.

Canonical `Platform.publish` does **not** use the semantic search seal protocol.
Semantic Page building separately seals generations and publishes its manifest
under the authority lock. The `search_generation` model covers its channel
publication requirements; neither model invents distributed atomicity.

Negative controls: `StaleRead` reproduces the omitted postflight guard and must
violate `ReadAuthorityCurrent`; `StalePublish` deliberately removes existing
publication fencing and must violate `PublicationFenced` (seeded, not a claimed
production bug). A positive state dump also reaches successful returns, so
rejection-only executions are not the claimed coverage.

Remaining obligations: arbitrary documents, content/ACL integrity, backend
faults/corruption, all Platform/search paths, concurrent transaction isolation,
real PostgreSQL/RLS behavior, process/power-loss recovery, and machine-checked
Python-to-TLA+ refinement. Existing component contracts remain required.

## Mandatory postflight structure lint

`semantic_manifest_checks.semantic_manifest_violations` follows the project's
existing presend/receipt AST checks. The ordinary checker and formal checker
both invoke it through formal lint, independently of source fingerprints. It
requires the reviewed synchronous shape: the original authority snapshot is
captured before external reads, is not replaced or passed to mutable helpers,
and the sole successful result exit is immediately preceded by the exact
unconditional postflight check. Exception exits raise; extra successful returns,
conditional/deferred checks, finalizer work and intervening operations fail.

Ten AST mutations are rejected after all affected source hashes are refreshed,
including removal, early/conditional guards, wrong or replaced/mutated snapshots,
early returns, extra work after the check, swallowed check exceptions and a
handler return. Equivalent AST formatting is accepted. The removal mutant still
produces the six independent public-contract behavioral failures. This is a
conservative structural constraint, not a general Python refinement proof or
protection against dynamic monkeypatching. Broader refactors require explicit
review and a corresponding lint/model/test update.
