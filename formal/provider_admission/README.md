# Persistent provider denial and checkpoint acknowledgement

This is **partial** owner-level coverage, not a claim that the `provider_admission`
family, all HTTP paths, or the entire repository is verified. The independent base
is `deps/formal-llm-outcomes` (`27c4180`), including PR31 and the formal gate. It
neither stacks nor reimplements PR34's capture/usage settlement changes.

## References reviewed before changing the protocol

- [Kubernetes API resource versions](https://kubernetes.io/docs/reference/using-api/api-concepts/#updates-to-existing-resources):
  a client-observed version makes a state-dependent update conditional and stale
  observations are rejected. Here the checkpoint similarly identifies the actual
  observed denial set, not merely its successfully persisted subset.
- [AWS circuit-breaker guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/circuit-breaker.html):
  persistent failure state prevents repeated calls and supports explicit operator
  intervention. This repository intentionally does **not** adopt periodic probes
  or automatic half-open recovery for permission denials.
- Existing `MeteredTransport._handle_admitted` observes and blocks HTTP 403 before
  reading its body or settling the ledger. PR34's reviewed implementation retains
  that order while separately tracking settlement acknowledgement. We reuse the
  ordering principle, not its unrelated capture/usage implementation.

## Two actual baseline counterexamples

1. `OpenAICompatibleAdapter.invoke` formerly settled the real SQLite physical
   ledger before `ProviderAdmission.block`. A controlled `finish` committed ERROR
   and then raised the original `CommitUnknown('settlement-commit-control')`.
   The response was already non-null, so the exception path skipped block and
   capture publication. Admission remained AVAILABLE, the ledger had no pending
   row, and a second invocation actually reached the MockTransport: **two sends**.
   Only one finish was attempted; the failure was not a fabricated lost update.
2. A durable error E1 produced checkpoint C. E2 was observed into `_ephemeral`,
   but its event write failed. The old checkpoint still equaled C because it
   hashed only files. Acknowledging C acknowledged E1 and E2, made the current
   instance AVAILABLE, and left an acknowledgement that failed validation after
   restart because E2 had no durable event.

Both reproductions used temporary synthetic inputs, actual filesystem/SQLite
operations, and no external model, embedding, judge or HTTP calls. The preserved
local baseline evidence is `.local/admission-baseline/`; it is not overwritten by
fixed-source runs.

## Minimal implementation

- A returned 403 response is recorded in memory and durably blocked **before**
  attempting physical settlement or fallible capture logging. A block-write error
  exits without settlement, retaining the durable STARTED reservation. A finish
  acknowledgement error keeps the original exception and operation ID as the
  cause; it is not retried. Post-settlement audit failure cannot remove the block.
- A checkpoint binds immutable journal content **and all observed error IDs**.
  Acknowledgement rejects a changed checkpoint or any observed event without a
  validated durable receipt. Successful controlled capture seeding can persist
  the missing event; a fresh explicit acknowledgement is then possible.
- Historical acknowledgement IDs must be a subset of **durable**, not merely
  process-local, event IDs. Existing valid append-only receipt files are unchanged.
  Invalid orphan history fails closed. Checkpoints are opaque current-state
  values: clients must reread after change/upgrade, not assume an older computed
  value remains valid across releases.
- An acknowledgement writes its checked snapshot IDs, not all events present at
  commit time. A concurrent new event is therefore still blocked. Operator ack
  performs no provider probe and changes no budget or original error record.

## Source mapping and finite scope

| Model | Concrete owner / transition | Bound and abstraction |
| --- | --- | --- |
| `ProviderAdmission` | `ProviderAdmission.block`, `snapshot`, `require_available`, `acknowledge`, `from_directory`, `seed_from_capture` | Two immutable event IDs, one operator observation/ack per process lifetime, one restart, at most two admission probes; durable versus ephemeral events, available versus unavailable storage, ack commit with or without lost acknowledgement. |
| `PermissionLatch` | `OpenAICompatibleAdapter.invoke` (also inherited by `AnthropicAdapter`) | One returned 403, sequential fresh invocations, two possible sends, one post-return restart; block publication and physical settlement are distinct failure boundaries. |

The latch model is not an atomic permission-check/socket-write model.
`OpenAICompatibleAdapter` checks availability **before** `reserve`; the physical
producer guard is held only within reserve. A request that passed admission before
another invocation's 403 can still be queued and later send. No stronger rule that
revokes already-admitted work is invented here. `MeteredTransport.handle_request`
holds its producer guard across its inner admission/send/settlement sequence;
its early 403 observation is source-linted but this batch does not prove the whole
transport or the composed native receipt protocol. Bare `_Transport`,
`GoogleEmbeddingProvider`, and `GoogleSemanticProvider` do not by themselves
construct this persistent latch; coverage of `observe_client`-bound Luna/Google
transport must not be generalized to those unwrapped owners.

A returned Response is also distinct from headers observed internally by httpx:
`client.send` reads the body by default. If that read raises before returning,
`response` remains absent and the existing UNKNOWN physical record conservatively
blocks another reservation. This batch does not refactor streaming or claim the
403 latch was published in that earlier body-error window.

The journal snapshot abstracts a returned validated observation, not transactional
filesystem enumeration. The no-probe/no-ledger-mutation model frame is supported
by actual contracts and lint, not a proof of external operator behavior. Latch
restart is after invocation return, not every possible instruction-level crash.

Pure `ProviderAdmission` cannot retain an event that never reached durable storage
across process death. The caller's still-STARTED physical ledger is the restart
backstop in `PermissionLatch`; the journal model makes no standalone durability
claim for lost ephemeral memory. Storage identity, immutable append semantics,
cryptographic collision resistance and OS/SQL guarantees are assumptions, not
proved by source hashes. Process restart, not power-loss durability, is modeled;
the file publisher fsyncs content but does not claim directory-fsync durability.

## Regression and mandatory lint

`provider_admission_formal_contracts.py` exercises actual SQLite budgets, file and
SQLite-plus-blob journals, fresh reopened storage handles, both HTTP adapters,
lost settlement acknowledgement, failed event/capture writes, stale/fresh
checkpoint refusal, controlled seed recovery, concurrent new event during ack,
ack commit acknowledgement loss, corrupt/orphan history, and unchanged original
error/budget evidence. All transports are synthetic. Existing adapter contracts
remain intact.

`provider_admission_rules.py` is invoked by the ordinary mandatory formal lint,
not an optional command. It checks ordering and fail-closed source structure;
eleven mutations refresh every reviewed source hash and must still fail the rule.
This is a structural regression guard, not a general semantic refinement proof.
The finite models separately need positive exhaustive exploration, named unsafe
counterexamples, and actual reachable successful-block/ack paths. A file hash
alone is never treated as evidence that a transition or invariant was checked.

The dependency base contains eleven pre-existing reviewed-anchor drifts from its
PR31 source delta. The refreshed inventory anchors cover send/error metadata in
`provider_capture`, `model_http`, `agent_model`, `contract_transfer`,
`provider_admission.require_available`, `provider_runtime.classify_failure`,
`knowledge_contracts.ModelEndpoint.complete`, and the two linked-budget summary
methods. The dependency also extracted `ReviewedPredecessorLedger._review_summary`;
its discovered state assignment is added as an unmodeled source-bound inventory
anchor, not suppressed. This is dependency source review only, not new model
coverage of those families.

## Validation record and remaining obligations

Development failures are retained: the first journal contracts incorrectly reused
an already sealed storage handle (two `DATABASE_PREPARE_CLOSED` fixture errors),
and one expanded targeted command omitted the native library path (45
`PLUGIN_NATIVE_UNAVAILABLE` errors). Corrected fresh handles and the explicitly
selected, hash-verified existing native library produced passing targeted runs;
neither initial failure is relabeled as a product defect or a passing run. An initial
TLC authoring run rejected an incomparable placeholder type and an unparenthesized
conditional RHS; both syntax/typing errors were corrected. The first unified
formal gate stopped on the pre-existing missing dependency state-owner inventory
entry before running TLC; that owner was added, not excluded.

Remaining owners include tool retry, physical multi-resource accounting, raw
transport rate/auth/format retries, complete capture recovery, operator evidence
truth, arbitrary concurrency and cross-owner composition. No new automatic retry,
TTL lock, probe, permission unblock, budget reset, security exemption or paid call
is introduced. The pre-freeze unified formal gate passed 19/19 runs (seven inherited gate/model
runs plus three safety positives, seven unsafe controls and two genuine success
witnesses here). Exact frozen-tree targeted/formal/ordinary validation is reported
separately; this result does not substitute for those required gates.
