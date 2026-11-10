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

## Task-specific embedding source view

The `EmbeddingSourceView` slice reviews the application at `f0ea7450`. A candidate
upload changes the current parse while the published manifest deliberately
continues serving the previous immutable version. `Platform.search` and the
Agent retrieval pipeline already used that published version, but
`KnowledgeAccess.validate_embedding` validated every task against the candidate.
An authorized dense query therefore failed with `STALE_BUILD` after an upload.

The source fix selects the published snapshot only for `task=query`; document
embedding continues to use the candidate for its staging build. Both snapshots
join current binding membership, source readers and processing policy. Existing
authority, manifest, exact source hash and final snapshot checks remain required.
The proof cache still checks current authority before accepting a cached proof.

| Finite action or value | Concrete boundary |
| --- | --- |
| `candidate`, `published`, `UploadCandidate` | Current candidate source/parse identity versus the immutable manifest source identity; `Platform.ingest` changes the candidate without publishing |
| `Stage`, `Publish`, `stageRevision` | A staging build is tied to current candidate/authority; canonical publication keeps its existing fence |
| `BeginRequest`, `auditVersion`, `auditBuild`, `auditRevision` | Public embedding audit for either a query of the current manifest or documents in the current staging build |
| `Admit`, `TaskSpecificSourceView` | `KnowledgeAccess.validate_embedding` selects the correct content view and does not reject a valid published query solely because a draft exists |
| `Revoke`, `AdmissionUsesLivePolicy` | Live ACL, active binding and task/provider processing policy must still allow the source |

The slice starts with one valid published source, has two versions, one optional
staging build, one request, one upload, one revocation and three authority values.
One Boolean abstracts the conjunction of the relevant current permissions.
Build identities are abstracted by their single source version. This does not
model multiple builds of the same version, multiple sources, parsing, partial
vector coverage, cache capacity or storage failures. Admission is one abstract
step whose implementation requires both snapshot and postflight fencing. It is
not an assertion that external reads or provider sends form a database
transaction. No fairness, prompt progress or delivery-time authorization is
claimed.

The `Candidate` control reproduces the wrong query content view and must violate
`TaskSpecificSourceView`. `FrozenPolicy` deliberately uses historical permission
and must violate `AdmissionUsesLivePolicy`; it is a safety control, not another
observed production defect. The two witness controls use the correct constants
and reach an admitted query of v1 and an admitted document build of v2 while the
candidate differs from the published version. They fail only their named
reachability-denial invariants, preventing rejection-only coverage.

`knowledge_embedding_contracts` exercises the actual SQLite authority,
EmbeddedSearch and public model/evidence JSON endpoints with deterministic
synthetic vectors. It covers resumable and legacy build paths, dense/hybrid/Agent
queries, new candidate document embeddings, publication, current readers,
binding removal and processing revocation. It checks exact source versions,
parse IDs, text hashes and zero provider sends after denial. These contracts do
not exercise real HTTP transport, PostgreSQL, remote embeddings or production
retrieval quality. The older `AuthorityPublication` slice still does not model
published parse joins; neither slice is a machine-checked source refinement.
