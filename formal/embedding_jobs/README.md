# Embedding jobs: bounded durable ownership model

This models `EmbeddingJobs` and its registered relational operations at baseline
`c8ef758`. It adds bounded safety coverage, not an embedding-provider implementation
or a claim that an embedding-job bug existed. Negative controls deliberately break
this model. The separate semantic-manifest change is not an embedding-job fix.

## Mature references inspected before modeling

- [Pinned DSH/Cordis `ce7073d`, `fiber.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/fiber.ts):
  asynchronous completion belongs to the activation epoch that issued it. The
  local analogue is a physical `request_id`, not a reusable batch number.
- [Temporal activity execution and cancellation](https://docs.temporal.io/activity-execution#cancellation),
  inspected 2026-11-02: cancellation communicates a request and may be ignored;
  it does not establish that an external effect never ran. `Cancel` therefore
  changes only local worker state, never the durable job or physical observation.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html), inspected
  2026-11-02: writes in one transaction commit together or roll back together,
  subject to the documented storage assumptions. `CommitClaim` and `CommitFinish`
  each atomically change the job and matching attempt. This model assumes, rather
  than proves, the database implementation's crash durability.
- [PostgreSQL 18 transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html),
  inspected 2026-11-02: a contended update re-evaluates its predicate against the
  updated row. Request identity, expected status and lease guards belong to the
  settlement transition, not an earlier unlocked snapshot.

No upstream application, remote embedding, language model or judge was executed.

## Source-to-model map

| Model | Existing implementation |
| --- | --- |
| `Prepare`, `dependency/frozen` | `EmbeddingJobs._prepare`; immutable dependency digest, provider manifest, ordered units and audit context; one abstract batch |
| `BeginClaim`, `CommitClaim` | `_claim`; `JOB_LOCK`, `JOB_CLAIM`, `ATTEMPT_INSERT` in one `job_transaction` |
| `attemptCount` | persisted attempt budget: claim increments; proven-unsent finish refunds; retryable observed HTTP failure is bounded at two attempts |
| `request/nextId`, `attempts` | fresh physical request identity and durable per-request attempt row; at most two distinct IDs in this model |
| `Send`, `Observe`, `physical` | the provider documents call and independently observed complete/error/known-unsent results; these are not database actions |
| `Cancel`, `Restart` | interrupted local invocation/process; an in-flight physical request may still complete; no product cancel API is asserted |
| `Elapse`, `Expire` | expired `RUNNING` becomes `UNKNOWN`, and `ATTEMPT_EXPIRE` marks the same request unknown |
| `BeginFinish`, `StatusAndLeaseValid`, `CommitFinish` | `_finish`, guarded `JOB_FINISH`, then `ATTEMPT_FINISH` in the same transaction; valid known-unsent may settle `RUNNING` or `UNKNOWN` without a live lease |
| `recover=TRUE` and `Observed` | `reconcile`, `recovered_observation`, `recovered_vectors`, `recovered_unsent`; exact capture/ledger/request/payload/manifest/usage and vector validation are abstracted as a trusted matched observation |
| `tx.op=ack`, `FinishTransaction`, `Restart` | precommit rollback vs committed rows with delivered or lost acknowledgment. A worker becomes active only after claim acknowledgment; restart never undoes committed rows |
| `replaySafe`, `settlementSafe`, `recoverySafe` | ghost history monitors; they remember a violation even if a later observation would make the current snapshot appear safe |

The SQL source's row/attempt transactions are the atomicity boundary. External
physical execution, database persistence, and caller knowledge are separate.
Ordinary HTTP errors represent observed retryable statuses, not all exceptions.
Ambiguous errors and expired/cancelled work cannot be blindly reclaimed.

## Checked scope and remaining obligations

The finite domain has one tenant/knowledge-base/build/batch, two fresh physical
request IDs, two dependency identities, two charged attempts, a binary lease,
four physical observation categories, and at most one database transaction.
Repeated rollback, acknowledgment loss, restart, cancellation and rejected stale
callbacks are allowed. All fields are bounded; a third request is not explored.
`UNKNOWN` is not a synonym for unsent or zero spend. Known-unsent evidence is
allowed only when no physical send occurred; the model does not establish the
trustworthiness of the provider that supplies this evidence.

The lease abstraction separates definitely-live from definitely-expired time.
It **excludes exact clock equality**: `_claim` uses `<=`, `JOB_FINISH` uses `>=`,
and `JOBS_EXPIRE` uses `<`. That corner remains an explicit unconfirmed obligation;
this model neither fixes it nor proves those three expressions equivalent.

Database isolation/locking correctness, serialization of multiple batches,
provider circuit state, arbitrary retry/backoff schedules, unbounded IDs,
cryptographic collision resistance, vector quality, actual provider cancellation,
exact recovery-payload validation and authorization changes are not proved here.
Expired/terminal caches are not assumed to be evidence of physical termination.
The retry count and physical-request ID count are distinct; known-unsent recovery
refunds the former but never reuses the latter. No fairness or eventual-success
property is claimed: an unknown request may remain unknown forever.

This is a manually reviewed abstraction, not a machine-checked Python/SQL
refinement proof. Source hashes detect drift requiring review, not equivalence.
The accompanying concrete SQLite contracts and existing recovery tests remain
necessary. A successful bounded run cannot establish zero implementation bugs.

## TLC and deliberate negatives

`EmbeddingJobs.cfg` checks `TypeOK`, `FrozenDependency`, `NoBlindReplay`,
`SettlementCorrelated`, `RecoveryHasEvidence`, `AtomicJobAttempt`, and
`CompletionBound`. All four negative configurations isolate one named invariant:

| Configuration | Seeded omission | Required violation |
| --- | --- | --- |
| `EmbeddingJobsFinishFenceNegative.cfg` | ignore request-ID match but retain phase/lease guards | `SettlementCorrelated` |
| `EmbeddingJobsRetryUnknownNegative.cfg` | allow claiming an unresolved unknown job | `NoBlindReplay` |
| `EmbeddingJobsRecoveryEvidenceNegative.cfg` | accept a completed recovery without a matched physical observation | `RecoveryHasEvidence` |
| `EmbeddingJobsAtomicCommitNegative.cfg` | commit job mutation without its attempt mutation | `AtomicJobAttempt` |

The stale-finish negative reaches a second active request and then accepts a
late known-unsent callback for the first request, incorrectly converting the
successor to retry. This is a seeded mutant, not an observed production defect.

The reviewed execution used the pinned TLA+ 1.7.4 jar (SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`), Java 21,
one worker, seed 1, fingerprint 0, 512 MiB heap, 128 MiB metaspace and a 120-second
per-run timeout. The formal gate regenerates evidence and checks exact exit codes
and framed violation events. Independent reachability probes also found ordinary
and recovered `COMPLETE` paths, so the positive checks are not limited to rejection
or dead-end initial states. Probe counterexamples witness reachability only; they
are not additional product defect claims or liveness proofs.

Final standalone model check: **12,277 generated / 3,235 distinct / 0 remaining**;
all four deliberate negatives exited **12** with their named invariant. The
ordinary and recovered-completion reachability probes also exited 12 against
intentionally false exclusion assertions. Earlier development parse/evaluation
failures are retained separately and are not counted as successful evidence.
