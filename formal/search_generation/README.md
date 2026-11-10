# Three-channel search generation publication

This model is a finite **partial safety abstraction**, not a new search runtime,
a distributed transaction, a liveness guarantee, or a proof of source refinement.
Baseline is `c8ef758`; this stage changes no search backend behavior.

## Mature patterns inspected first

The [pinned DSH/Cordis effective-config publication guards](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/fiber.ts)
distinguish candidate preparation from fenced publication. [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
and [PostgreSQL 18 conditional update semantics](https://www.postgresql.org/docs/18/transaction-iso.html)
inform the single-owner commit/CAS boundary. These primary pages were inspected
2026-11-02; they do not establish atomicity across independent channel stores.

## Concrete mapping

| Model | Source |
| --- | --- |
| `content[g][source/bm25/dense]`, `Stage` | `SplitSearch.stage`, `DocumentEvidenceSource.stage`, channel adapters and `EmbeddedSearch.stage` |
| `Canonical` | `SplitSearch._read_generation/verify`: canonical records, count, source hash, dimension and storage identity |
| `Seal`, `frozenContent` | `SplitSearch._seal_token/seal/is_sealed`, immutable evidence seal and `EmbeddedSearch.seal/is_sealed` |
| `BeginPublish` | `SplitSearch.publish_manifest`, then `DocumentEvidenceSource.publish_manifest` reading the existing pointer revision |
| `CommitPublish`, `RejectCAS` | document store `put(..., expected_revision=...)`; a single pointer update, not a three-store transaction |
| `publicationSafe/canonicalSafe/casSafe` | ghost monitors recording conditions at each accepted publication |

Two generations, three channels, two abstract normalized content identities,
three pointer revision values, at most one publication per generation. Identity
includes the bound generation, scope, canonical content, vector dimension,
dataset and checksum relationship; it abstracts exact serialization and assumes
no hash collisions. A staged mismatched channel is an injected backend fault:
verification must reject it. The model does not claim the trusted backend
ordinarily generates mismatches.

Each channel stage/seal commit is separate. A partial stage or seal remains
unpublished; retry can fill absent channels without replacing existing content.
Failures before committing a channel/pointer are stuttering durable-state steps.
The request-local `pending` CAS snapshot is distinct from the durable pointer;
`Restart` discards it without manufacturing a commit or erasing durable content. Process
restart, partial files, fsync, filesystem rename, and uncertain acknowledgments
are **not** refined into a filesystem crash model.

`SealedContentImmutable` binds sealed values to their frozen content. Existing
real temporary-storage contracts cover incomplete channels, half-seal refusal,
restart, mismatched content, exact dataset binding and immutable readback:
`SearchRouterContracts.test_half_seal_is_not_published_and_retry_completes_without_replacing_channels`
and related `search_router_contracts` tests. They are reused, not replaced by a
new test harness. No actual backend bug was confirmed in this scoped review.

Three deliberately broken configurations remove respectively seal completeness,
canonical readback, or the pointer CAS fence. They must violate their respective
named invariants. The positive state dump reaches an active manifest. There is
no fairness/liveness assertion: permanent storage failure can prevent progress.

Remaining obligations include semantic-page capsule/source authorization, all
adapter implementations, concurrent file writers, real database isolation/RLS,
storage transport/protocol behavior, persistence under power loss, unbounded
generations and a machine-checked source refinement. Canonical relational
`Platform.publish` is a different authority and does not require this seal.
