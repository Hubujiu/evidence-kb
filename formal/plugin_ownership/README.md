# Plugin protocol ownership: additional finite slices

Base: `27c4180e1a66d0b7c9d554eff9620ef16a49023b` (PR31 and PR32 dependency
integration). This independent batch leaves the existing
`formal/plugin_lifecycle/PluginLifecycle.tla` and its superseded-initialization
fix unchanged. Stages 1–3 are model-only; stages 4–5 separately reproduce and fix
concrete revision-convergence and dynamic-binding defects. Seeded mutants alone are not bug evidence.

## Mature reference before modeling

The pinned [DSH Cordis fiber](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/fiber.ts)
was read at `_refresh` (all required implementations contribute to the epoch),
`_reload` (committed dependency identities), and `_unload` (dependent teardown
before provider release). The matching
[events implementation](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/events.ts)
was read for owned registration, snapshot dispatch and listener cleanup.
`config/plugin-runtime/cordis-reference-lock.json` pins these original files;
this task does not execute upstream JavaScript or paid providers.

This project's Rust protocol is not an exact Cordis port. It explicitly refuses
parallel sibling executions of a serial plugin, permits admitted nested work,
retains resources until real completion, and stops cleanup at a failed resource
until explicit retry. Upstream cleanup logs individual disposer errors and uses
concurrent promise joining; it does not establish the stronger Rust retry/LIFO
contract. See `docs/plugin-runtime.md` and `docs/plugin-harness.md`.

## Stage 1: ScopeLeaseAdmission

Three initially active owners occupy a root and child scope. The child consumer
holds two distinct provider identities. A separate runtime is represented by a
foreign-owner admission attempt. Four never-reused lease identities include an
optional scope pin and up to three serial executions; parent ancestry is copied
at acquisition and remains valid even if the parent later completes. Requests
are atomic coordinator commands, not atomic callback/network executions.

| Model action or state | Actual owner |
| --- | --- |
| `ScopePin`, `Call`, immutable allocated lease/ancestor identities | `Coordinator::acquire`, `Lease` |
| `ForeignCall` | runtime and scope ancestry guards in `acquire` |
| `Cancel`, `Drain` | `cancel`, `drain_instance`, `drain`; notification only |
| `Tick` | `RuntimeTick`, inherited minimum deadline check in `acquire` |
| `Complete` | invoke completion / non-operation scope `RuntimeRelease` |
| `bindings`, `CleanConsumer`, `CleanProvider` | `retained_by_consumer`, `closing_scope_busy`, cleanup eligibility in `reconcile` |

Safety checks enforce comparable ancestry for simultaneous serial executions,
no foreign-owner admission, live-lease cleanup pins, and retention of both
providers until consumer release. The two success witnesses require an actual
cancelled parent to admit a nested child and then allow all owners to drain after
real completion. This intentionally does **not** assert that cancellation
revokes the whole admitted chain. Expired deadlines still reject new work.

The four negative profiles independently remove serial, scope, live-work or
consumer-binding protection. A named invariant violation with TLC exit 12 is the
expected negative result; parser errors, timeouts, zero-state runs and unrelated
invariants cannot pass. Success witnesses also intentionally exit 12, but prove
reachability of the named positive outcome rather than a bug.

Actual Rust contracts in `ownership_tests.rs` combine a cancelled child-scope
serial chain, both retained dependencies, refused sibling execution, deferred
cleanup, foreign runtime rejection, and inherited deadline expiry. Existing
contracts for nested inherited calls, serial ancestry, visibility/isolation and
real completion remain in `tests.rs` and are not removed or weakened.

The mandatory `formal_plugin_ownership` token-shape lint is called by the ordinary
formal registry gate. Its eight source mutations refresh source-binding hashes
before invoking that gate on the actual plugin source and registered model
slice. This is a semantic review tripwire, **not** a Rust parser, compiler-level
proof or a guarantee against every syntactically different unsafe implementation.

## Stage 2: ActivationDispatch

This slice starts with two unavailable dependencies and one pending instance.
Each dependency becomes ready independently; init is queued only after both are
bound. `Claim` represents `RuntimePoll`, not callback completion. One disable
and one re-enable can race a queued or claimed init. Unclaimed cancellation
removes both the queued action and its operation identity; claimed work keeps
ownership until actual completion and cannot publish into the replacement epoch.
`Start`, `Disable`, `Enable`, `CancelInit`, `Complete`, and `Clean` map respectively
to `reconcile`, `update`, `cancel_unclaimed_init`, `cancel`, and `complete`'s init
and final-cleanup arms. IDs 1 and 2 abstract distinct init operations/generations;
allocation history is never reset or reused. Cleanup resource details are deferred
to stage 3, not silently assumed proved by this abstraction.

Four independent negative profiles target incomplete dependency admission,
dispatched suppressed init, stale-epoch publication, and generation reuse. Three
success witnesses reach unclaimed replacement recovery, claimed replacement
recovery, and an explicitly cancelled init's late success remaining failed.
All three were actually reached. The positive profile explores 82 states; this
small bound intentionally includes only two generations, so a re-enable after
cleanup has already advanced to generation 2 (which requires generation 3 in the
implementation), repeated updates, and final-generation teardown are outside it.
It makes no liveness claim or claim that every command succeeds.

Two additional actual Rust contracts cover both dependencies gating dispatch,
unclaimed disable/re-enable, claimed late success, resource retention, new action
identity, duplicate old completion not changing the successor, and stale-generation
admission rejection. Six additional mandatory source-shape mutants are rejected
after source hashes are refreshed. The full native plugin suite passes 50 tests;
20 gate/lint tests and all 22 unified formal runs plus two evidence contracts pass
in `.local/*stage2*`. An interrupted authoring tool before registration produced
no stage-2 test logs or completed validation; only the subsequently completed
runs are counted. No production coordinator change was needed.

## Stage 3: FinalizationOwnership

One active child instance owns three ordered resource identities and one already
claimed invocation; one ancestor listener remains independently active. Cleanup
operations use monotonically allocated IDs 3 through 7, with one possible failure
and retry. ID 0 is a foreign terminal operation and IDs 1/2 are child init/invoke.
The fixed IDs abstract identity only; they are not copied across owner boundaries.

`Register`, `ReadListeners`, `Schedule`, `ClaimCleanup`, `FinishCleanup`, `Retry`,
`Ack`, `CompleteAgain`, `Retire` and `Prune` map to `resource`, `listeners`,
`reconcile`'s reversed resource selection, `RuntimePoll`, `complete`, `retry`,
`ack`, immutable completion handling, `apply`'s retired set and `prune_retired`.
The model allows an invocation to register a late resource while draining until
real completion, but never after that operation completes. Listener results are
snapshots, not live mutable enumerators or proofs of callback execution.

The seven seeded profiles target FIFO cleanup, failed-resource release, pending
or foreign acknowledgement, completion overwrite, premature retirement and wrong
listener sorting. Three positive-outcome witnesses reach complete failed-cleanup
retry/ordered resource removal/ack/retirement, a failed cleanup retaining its
resource, and both mixed-batch ack rejections without deleting the earlier valid
operation. The full success trace has resources `{2,3,4}` done, only foreign
operation 0 retained, and distinct failed/retry operation IDs; duplicate completion
and conflicting completion rejection are both actually reached.

Positive exploration has 17,800 states. All 33 unified formal runs and two evidence
contracts pass. The native plugin suite passes 52 tests, including six new combined
real coordinator tests. Twenty gate/lint tests pass, with 23 refreshed-hash source
mutations across the three slices. Raw authoring evidence is in `.local/*stage3*`.
The production coordinator is byte-identical to the dependency base; this is
model-only, not a newly reproduced implementation bug.

## Stage 4: overlapping revision convergence (confirmed defect)

Before this change, actual Rust commands updating root provider P and then
unrelated Q replaced the first update's affected descendant set. After P/Q became
active but P's child consumer was still executing init, root inspection reported
`desired_revision = observed_revision = 3`, `operation.status = complete`, and
only Q affected, while one child operation was still pending. The real baseline
regression ran one test and failed (exit 101); `.local/plugin-revision-baseline.log`
contains the actual snapshot. The earlier incorrectly unqualified `--exact` filter
ran zero tests and is retained separately as a diagnostic, never as a PASS.
The baseline coordinator/test hashes and test patch are retained alongside it.

The mature references were reviewed before this revision fix: pinned DSH fiber
`_setEpoch`/`_unload` retain in-flight/dependent ownership, and
[Kubernetes v1.34.1 DeploymentComplete](https://github.com/kubernetes/kubernetes/blob/v1.34.1/pkg/controller/deployment/util/deployment_util.go#L687-L694)
requires actual target availability as well as generation observation.
[Kubernetes observedGeneration](https://kubernetes.io/docs/reference/kubernetes-api/apps/deployment-v1/#DeploymentStatus)
alone is not readiness. This project deliberately has its own stronger
`reconcile` rule that publishes observed revision when the scoped target and its
affected descendants reach their desired phases; the two protocols are not equated.

`revision_affected` now carries the previous affected closure only while the
previous revision is still unobserved, then computes the new transitive closure.
Both `update` and `apply` use it. An empty change set remains empty so identical
apply stays idempotent. Already converged history is not retained forever; no
work is silently cancelled, completed or removed to manufacture convergence.

`RevisionConvergence` uses fixed P/Q/consumer identities, two scopes, four root
revisions and target versions up to three. It represents observation separately
from internal progress; no fairness or implementation refinement is claimed.
The legacy-overwrite profile reaches the exact premature-observation invariant.
Three success witnesses reach overlapping completion, a later unrelated update
while an independently reconfigured old consumer is pending, and convergence
after legitimate child removal/collection. `CollectConsumer` abstracts completed
removal; it does not prove the internal collection authorization/cleanup protocol.

The real regression exercises both update and apply, plus repeated no-op apply,
with completion/removal/subtree collection outcomes (six controlled runtimes).
The completion paths then verify that a later unrelated update does not remain
blocked by historical child ownership. All native plugin tests pass (53 total).
The unified runner passes 38 expected runs plus two evidence contracts; the new
positive graph has 89 states. The 20 targeted gate/lint tests include 28 refreshed-hash
mutants. Source guards require both entry points, conditional pending carry,
transitive closure and no-op identity. No ordinary full gate is claimed yet.

## Stage 5: dynamic visibility and admitted definition identity (confirmed defects)

A real root registration retained by a child scope lease remained callable after
installing a local shadow. The old `invoke` admitted the retained root instance,
then re-resolved the service name to construct its action binding. With an active
shadow, the action carried the old instance but the new registration/definition
and lost the ancestor intercept. With a still-starting shadow, this late lookup
returned `service_inactive` after allocating an unreturned lease and operation.
Both actual one-test baseline runs failed (exit 101), separately recorded in
`.local/plugin-visibility-baseline.log` and
`.local/plugin-visibility-pending-baseline.log`; neither is a seeded-mutant claim.

The pinned [Cordis reflect implementation](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/reflect.ts)
was inspected before changing this path: committed implementation identity,
`getTraceable`, `_getImpl`, and provider release keep definition ownership distinct
from current name visibility. Its SHA-256 matches the existing reference lock.
This does not import upstream semantics that differ from this protocol.

The minimal fix constructs binding metadata from the exact registration instance
already admitted by `acquire`. `registered_binding` is shared with ordinary
lookup, but introduces no new admission bypass: fresh calls still check visibility,
isolation and activity; retained calls still require their real parent lease.
The admitted definition determines host, generation, registration and the ancestor
intercept chain. No post-allocation name lookup can fail or switch providers.

`DynamicVisibility` bounds two immutable registrations, two never-reused call IDs,
one scope pin and one local shadow lifecycle. Two positive profiles separately
exercise normal and isolated scopes. Five named negatives cover the two real
legacy failures, isolation bypass, inactive-shadow fallback and lost ancestor
intercept. Two reached success witnesses require retained calls during both pending
and active shadow followed by explicit completion/disposal, and a legitimate local
call under isolation. The intercept abstraction records root participation and
child override; arbitrary recursive JSON merge is not proved by this model.

All 55 actual Rust plugin contracts pass, including both new regressions. Twenty
mandatory gate/lint methods pass with 36 refreshed-hash source mutants; all 47
unified formal runs and two evidence contracts pass. Raw stage-5 evidence is in
`.local/plugin-*stage5*` and `.local/formal-stage5/`. Ordinary full is still NOT_RUN.

## Stage 6: stored-parent subtree reclamation (model-only)

The pinned Cordis fiber `_unload` and structural disposer were re-read for
waiting on actual dependent teardown before release. Rust additionally keeps
explicit terminal records until `collect`; upstream does not prove that protocol.
`SubtreeCollection` represents root/child/grandchild plus a foreign runtime, a
fresh never-reused scope, five operation records including an ancestor-owned
invocation whose **call scope** is the grandchild, one live invocation and one
scope pin. `Drain`, `Complete`, `Release`, `Dispose`, `Reject`, `Collect`, and
`Create` map to `drain`, real invoke completion, release, `reconcile`'s child-edge
detachment, collection guards, `collect`, and monotonic `allocate`.

Logical child edges disappear after completed disposal, but stored parent links
remain until explicit collection. The model therefore checks transitive parent
closure, no collection before real completion, both operation owner/call-host
cleanup, affected/diagnostic cleanup, foreign survival and identity non-reuse.
Six named seeded negatives violate those checks; two reached success witnesses
require an earlier busy rejection, real invocation completion and pin release,
then nested or root collection and a genuinely fresh handle. Cleanup callbacks
and their retry/LIFO details compose with stage 3; `Dispose` assumes they have
really finished and is not a substitute for that proof or for external progress.

The positive graph has 64 states. All 56 actual Rust tests pass, including two
new controlled runtimes that collect root or child after detached grandchild
cleanup while preserving another runtime unchanged. The first test-authoring run
incorrectly expected unknown-ID `ack` to fail; actual ack intentionally tolerates
already-forgotten IDs. That FAIL is retained, and the contract now uses `cancel`
plus operation counts to observe actual absence without changing production.
An authoring hash-refresh script used the wrong helper signature; the resulting
stale-source lint FAIL is also retained separately, not counted as verification.
After correction, 20 targeted methods / 44 refreshed-hash mutants and all 56
unified formal runs plus two evidence contracts pass. This stage changes no
production source and establishes no new implementation defect.

## Stage 7: exit-notice incarnation identity (model-only)

Pinned Cordis `_reload` checks the captured epoch before assigning a late error;
its structural disposer also joins actual teardown. That mature identity pattern
was reviewed before this separate Rust notice model, not treated as a port.
`ExitIdentity` maps `Publish`, `Update`, `Remove`, `Finish`, `Readd` and `Notice`
to completion, reconfiguration, removal/cleanup, apply's fresh instance allocation,
and `instance_exited`. Two never-reused instance identities can each have generation
1; generation alone therefore cannot identify an incarnation. A replacement also
retains the first instance while advancing to generation 2. Wrong scope/plugin
probes address the modeled target, not arbitrary foreign-owner exit processing.

Four seeded negatives remove scope/plugin lookup, instance identity, generation,
or live-phase matching. Three reached witnesses require a stale-generation notice
ignored before valid successor exit, and stale old-instance/namespace notices
ignored before valid **readded** Starting or Active exit plus duplicate rejection.
The Starting/Active flags explicitly belong to the readded identity, preventing
an earlier successful branch from satisfying a later witness. Enabled-state
checking remains a mandatory source guard; in this bounded graph it is redundant
with phase eligibility rather than a claimed independently reachable negative.
Cleanup and dependent convergence are composed assumptions from earlier slices.

The additional real test runs both Starting and Active cases after remove/readd,
checks equal generation but unequal instance ID, rejects wrong host/plugin/instance/
generation without mutating either runtime, and confirms valid exit disables only
the current owner. All 57 native contracts, 20 targeted methods / 50 refreshed-hash
mutants, and 64 unified formal runs plus two evidence contracts pass. No new
production defect was reproduced in this owner. Ordinary full remains NOT_RUN.

## Stage 8: bounded diagnostic ownership and retention (model-only)

[Tokio 1.47.1 broadcast](https://github.com/tokio-rs/tokio/blob/tokio-1.47.1/tokio/src/sync/broadcast.rs)
was read first for its documented bounded-buffer policy: a new item at capacity
releases the oldest retained item. This protocol is not a broadcast receiver or
audit log and has no lag notification guarantee; only that bounded FIFO reference
is applicable. Rust deliberately caps diagnostics **globally**, not per runtime.
A valid event from another runtime can evict an older root event. Inspection still
filters runtime identity, while subtree collection removes concrete-host events.

`DiagnosticRetention` bounds three hosts/two runtimes, capacity two and five
immutable event identities. `Emit`, `Inspect`, and `Collect` map to `diagnostic`,
`inspect` and the diagnostic arm of `collect`. Payload/identity validation occurs
before mutation; a rejected event must not evict a prior event at capacity. An
independent correct-policy queue makes oldest-versus-newest eviction observable.
Six named negatives cover identity, reject-time mutation, capacity, FIFO,
inspection isolation and child collection. Two reached witnesses combine a
foreign valid eviction, invalid rejection at capacity, both runtime projections,
child collection and successful subsequent root append. Capacity is not a proof
of durable logging or of preserving all historical failure facts indefinitely.

The positive graph has 88 states. The real contract fills the actual 1024 limit,
rejects foreign/unknown identities and invalid payloads without changing either
projection, accepts same-runtime child references to root operations, verifies
cross-runtime oldest eviction, then collects only the child's records. All 58
native tests, 20 targeted methods / 56 refreshed-hash mutants and 73 unified
formal runs plus two evidence contracts pass. The mandatory token lint now also
checks identity validation precedes the **first** eviction, with an actual early-
eviction mutation; merely finding both code fragments is insufficient. No new
production defect was found, and ordinary full remains NOT_RUN.

## Stage 9: explicit failed-initialization retry (model-only)

Pinned Cordis `_setEpoch` refuses to recover a failed fiber while `_error` remains;
its explicit update path clears that failure before a new activation. Rust has
an explicit `retry` command instead. That mature terminal-failure distinction was
read first, without importing different configuration/update semantics.
`FailedInitRetry` separates current init failure, cleanup failure/retry, terminal
Failed, explicit init retry, fresh generation/operation and successful activation.
`FailInit`, cleanup actions, `RetryInit`, `Dispatch` and `DuplicateOld` map to
`complete`, cleanup/retry, `retry`'s Failed branch, `reconcile`, and immutable
completion replay. Cleanup resource details are composed with stage 3.

Four seeded negatives target automatic recovery, uncleared failure, reused
generation and dispatch into a closed scope. The closed-scope negative abstracts
both desired-state and open-ancestor defenses; neither is claimed independently
reachable when the other remains intact. Three reached witnesses require real
failed cleanup retry plus explicit activation recovery, terminal failure retained
before that retry, and a closed scope accepting local failure reset while still
refusing any new init dispatch. The original error receipt remains unchanged.

Two actual controlled runtimes verify selected-owner versus all-owner retry,
unknown selection without mutation, generation increment, fresh operation identity,
old completion replay leaving the successor untouched, and closed-scope retry
without reopening or admitting a child. Existing current-error/cancelled-init
retry tests remain unchanged. All 59 native contracts, 20 targeted methods /
61 refreshed-hash mutants and 81 unified formal runs plus two evidence contracts
pass. This stage is model-only, not a newly reproduced bug.

## Stage 10: initialization and cleanup handle authority (model-only)

The pinned Cordis committed implementation and captured epoch checks were re-read
before this Rust-specific authority slice. `AuthorityHandles` distinguishes queued
from claimed initialization, a directly acquired initialization lease and inherited
lease, cancellation from actual completion, and two distinct cleanup attempts.
`AcquireInit`, `Borrow`/`ProbeClosed`, `CompleteInit`, lease release and cleanup
map to `acquire`, inherited initialization closure, `complete`, release and
`reconcile`. `Resolve` checks scoped declared consumer, generation, current cleanup
operation or live consumer phase. The provider remains a fixed retained identity.

Cancellation can still admit nested work on the already-admitted chain before
actual initialization completion. Completion closes that inherited authority,
but does not release its real leases or let cleanup overtake them. A failed
cleanup's old operation cannot authorize dependency lookup during its retry;
only the current cleanup identity can. Cleanup lookup is not modeled as requiring
claim because the actual guard checks current operation identity, not that flag.
`ProbeClosed` tests another authorization request after the two bounded lease IDs
are allocated; it does not allocate or reuse a third identity. Resource registration
is composed with stage 3 and guarded by live claimed current-generation operation.

Nine named negatives target initialization identity/claim/phase, inherited closure,
live lease pins, and consumer generation/owner/current-cleanup/live-phase checks.
Three reached witnesses combine cancelled-but-still-admitted nested work followed
by completion closure, active consumer replacement with stale cleanup rejection,
and initial unclaimed rejection followed by genuine claimed authority. The positive
graph has 1,320 states. The real queued-init contract probes a finite synthetic ID
range, then asserts the subsequently polled actual operation was within that range;
therefore it genuinely exercised unclaimed rejection without a production hook.
The companion contract verifies cleanup retry identities and final replacement
binding, with invalid resolution requests leaving state unchanged.

All 61 native tests, 20 targeted methods / 69 refreshed-hash mutants, and 94 unified
formal runs plus two evidence contracts pass. An initial lint authoring FAIL found
that `!record.claimed` still contained the short `record.claimed` fragment. The
mandatory guard was strengthened to include its enclosing predicate prefix; the
mutant now fails after hash refresh. The original FAIL is retained and no production
source changed. Ordinary full remains NOT_RUN.

## Explicit remaining work and boundaries

This feature is still WIP. Concrete next owners, not merely external assumptions:

- multi-scope bounded RuntimePoll dispatch, queue ordering and rest preservation.

Their source bindings are inventory/review anchors only until separate registered
models and actual contracts cover their transitions. Remaining work is not
silently declared covered by the ten existing slices.

Unbounded scopes/plugins/calls and arbitrary recursive intercept payloads,
arbitrary callback effects, process transport, allocation exhaustion, OS failure,
and implementation refinement remain outside the finite scope. The model has
no fairness/liveness assertion; terminal deadlock checking is disabled deliberately.
A positive safety result and a happy-path witness do not prove all branches are
reachable or that cooperative callbacks must finish.

The base contained eleven stale Python source hashes after PR31 composition and
one newly extracted `ReviewedPredecessorLedger._review_summary` inventory owner.
Their actual symbols were reviewed and inventory bindings refreshed. This does
not add model coverage for budget, RPC, provider admission or cache owners.

## Evidence

Stage 1 authoring validation: `cargo test --locked --offline -p plugin-runtime`
passed 48 tests (including two new combined protocol contracts); the mandatory
formal registry/verdict/lint contracts passed 20 tests, including eight refreshed-hash
source mutations. The unified formal runner passed all 14 registered runs and
two evidence contracts. ScopeLeaseAdmission explored 149,994 distinct states;
four named negative profiles and both positive-outcome witnesses were reached.
The initial formatting check failed on the new Rust test formatting and was corrected;
an earlier shell redirection failed before Java started, and is not a model run.
Raw authoring evidence is retained in `.local/plugin-*stage1.log` and
`.local/formal-stage1/`. Final-tree verification follows after the concrete remaining owners;
no ordinary full gate or remote CI is claimed yet.
