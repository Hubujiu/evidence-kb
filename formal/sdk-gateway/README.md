# SDK gateway ownership and recovery

These are finite safety models of `SDKGateway`, not a proof of the complete
PageIndex SDK, HTTP server, budget database, or StageCache. The request owner is
`SDKGateway.__init__.Handler.do_POST`; recovery initialization, `stage_key`, the
context property, and `close` are distinct mapped boundaries. The caller in
`pageindex_full_index.main` supplies one document context at a time and closes the
gateway before publishing its final budget summary.

## Established patterns used

- [RFC 9110, section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#name-idempotent-methods)
  distinguishes safe retry decisions from an unobserved result. This application
  gateway does not automatically repeat a POST after an unknown physical result.
  Recovery is an explicit new run: exact settled results are reused, while a
  permitted terminal failure creates a new physical identity and a `retry_of`
  relationship. This is an application replay cache, not a general HTTP cache.
- [RFC 9111, sections 2 and 4](https://www.rfc-editor.org/rfc/rfc9111.html)
  treats request target identity as part of deciding whether an old response can
  answer a new request. This application replay mechanism reuses the existing
  `ProviderAdmission.scope_hash`: provider id, model, dimension, and semantic
  endpoint identity. Every recovery capture must match that scope before either
  cache publication or retry-lineage selection; a missing scope fails closed.
  It does not substitute a configuration filename or file hash, add a credential
  hash, or claim to implement general HTTP POST caching rules.
- [Python 3.13 `socketserver`](https://docs.python.org/3.13/library/socketserver.html)
  documents that `shutdown` stops the serving loop and `server_close` waits for
  non-daemon request threads when `block_on_close` remains enabled. The gateway
  owns its handlers, server loop, HTTP client, and recovery cache. Client close is
  not evidence that a handler has finished capturing or settling a request.
- [HTTPX transports](https://www.python-httpx.org/advanced/transports/)
  provide the existing `MockTransport` seam for deterministic provider responses
  without paid or remote calls. HTTPX's
  [extensions interface](https://www.python-httpx.org/advanced/extensions/)
  provides a place for this project's custom `physical_request_id` response
  extension. `MeteredTransport` supplies that identity; HTTPX itself does not
  create or validate it. This avoids rediscovering identity by scanning
  same-payload files.

## Models and bounds

### `SDKGatewayRequest`

Two payloads, two complete context identities, two physical attempts, and at most
two handler operations. The contexts can stand for stages of the same document,
so a document-only comparison cannot substitute for an exact context key.
`Admit` snapshots context and linearizes the seen/cache/global gate decision.
`Send`, provider observation, settlement acknowledgment, exact cache publication,
local-error projection, and response completion are separate actions. A context
change while a request is active conservatively includes a reentrant callback;
ordinary cross-thread setters serialize through the gateway lock.

`RequestClose` is merely the caller invoking close. `BeginClose` represents the
later acquisition of the handler lock and setting the closing gate, after the
handler's capture work and response decision have finished. `FinishClose` waits
for the handler response phase too. A request admitted before a separate gate
closure need not be rolled back; the invariant forbids new admission after that
linearization, not completion of already admitted work.

The seeded controls isolate:

| Configuration suffix | Mutation | Violated invariant |
| --- | --- | --- |
| `StaleStatus` | Keep upstream 200 after local cache/report failure | `ErrorBodyIsFailure` |
| `WeakBinding` | Bind using all completed records with the same payload | `NoSpuriousCrossContextRejection` |
| `MutableContext` | Publish the mutable global context instead of the request snapshot | `ReportedContextStable` |
| `EarlyClose` | Close owned resources while an admitted capture is active | `ClosedIsQuiescent` |
| `IgnoreGate` | Admit a new operation after the global rejection gate closes | `NoAdmissionAfterGate` |

`ReplayWitness`, `TwoContextsWitness`, and `DrainWitness` deliberately negate a
reachable outcome while all mutation flags remain false. They must fail only
after actual transitions: an exact cache reuse, two distinct contexts captured,
or close requested during capture followed by orderly quiescence. They are
reachability witnesses, not product bugs and not initialization-only controls.

### `SDKGatewayRecovery`

Two payloads, two contexts, two provider scopes, one earlier physical identity
and one possible new identity. The old request passes through send, provider observation, capture
settlement, optional crash, recovery load, and a subsequent request decision.
Known HTTP response, capture status, and settlement acknowledgment are separate.
A known 200 with an unconfirmed ledger settlement is still unresolved. A settled
non-200 success capture is not treated as a replayable chat-completion 200.
Unknown or STARTED predecessors reject recovery rather than authorizing a new
send. An exact committed 200 can be reused without a new physical send. A
terminal HTTP error can authorize a new identity with the old identity recorded
as its retry origin. Different exact context/payload pairs cannot borrow that
origin or response. A provider-scope mismatch rejects the recovery run before
cache or retry decisions, even when its old result is a confirmed HTTP 200.

`TrustStatus`, `RetryUnknown`, `PayloadOnly`, and `IdentityReplay` independently
remove the settlement gate, resend an unknown result, weaken the recovery key,
and reuse the physical identity. Each targets a named safety invariant. `IgnoreProvider` removes only the provider-scope guard; `ProviderWitness`
reaches rejection of a confirmed 200 from a different provider scope. The four
other witness configurations establish actual completed reuse, explicit new-identity
retry, rejection of a known-but-unconfirmed response, and rejection of an unknown
result with the safe configuration.

### `SDKGatewayLineage`

At most two old records in one exact key group, with context/payload identities
each drawn from a two-value domain. Both record input orders are explored. All
records in this small model are already integrity-checked, terminal, and
settlement-confirmed; the preceding recovery model checks that admission gate.
At most one COMPLETE result can be reused. An all-ERROR group must have a unique
leaf whose acyclic predecessor walk covers every supplied record. A predecessor
outside the supplied group is an opaque historical root, not a fabricated third
attempt. Selecting an old request by filename order is not a lineage relation.

`FilenameOrder` retains the validity check but selects the final input record,
showing that valid-chain selection can still be wrong. `UniqueLeafOnly` omits the
full walk and accepts a disconnected self-cycle plus an apparent leaf.
`LeafWitness` reaches the correct leaf despite reverse input order;
`RejectWitness` reaches rejection of the disconnected cycle.

## Executable boundary

`SDKGatewayContracts` uses the real SQLite budget and StageCache with the existing
`MeteredTransport` and synthetic HTTPX provider responses. It drives the actual
handler without sockets in the sealed offline worker. The nested `live_suite`
is instead registered in the controlled `host-integration` phase: it exercises
real loopback HTTP, in-flight handler shutdown, and the actual
`pageindex_full_index.main` with a synthetic SDK. These are not paid-provider or
installed-third-party-SDK acceptance results. Both index and query SDK construction are inside the
caller's `try/finally` so its failure still drains the gateway before budget
publication. Mandatory AST checks cover these boundaries even after review
hashes are refreshed; model source digests are additional review alarms, not
substitutes for those checks.

## Mapping limits and remaining obligations

- The models abstract hashes as exact identity and StageCache authorization as
  already checked. Actual digest construction, retry-metadata normalization,
  immutable source/PDF checks, response-body integrity, and file identity checks
  require the source lint and executable contracts. No cryptographic theorem is
  claimed. Provider scope is frozen for one gateway instance; credential rotation,
  hidden provider revision changes behind the same declared model/endpoint, and
  arbitrary mutation of the provider object are not modeled.
- `SDKGatewayRequest` assumes a successful `MeteredTransport` send has a durable
  reservation. Database atomicity, multidimensional budget arithmetic,
  admission-control persistence, and missing token charges remain the separate
  physical-budget owners' obligations. A transport `ERROR` can also describe a
  known-zero-send connection failure; the recovery model's ERROR path is the
  narrower terminal HTTP-response case, not an assertion that every ERROR was
  received from a provider.
- A settled capture can exist even when cache/report publication or HTTP response
  delivery fails. The models do not erase that capture, infer a zero charge, or
  permit an automatic resend. Network delivery, arbitrary bytes, filesystem
  durability/power loss, capture-write failures, and external provider behavior
  still need executable integration tests and operational handling.
- The loopback endpoint is for the trusted local evaluation caller, not an
  authenticated multi-tenant service. No browser exploit or new authentication
  scheme is asserted. The caller sets context in operation order and waits for
  each SDK operation to finish; a third-party SDK issuing background requests
  after its operation returns is outside this correlation contract. `close` is
  called by the controlling thread, not by an owned handler. Ordinary HTTP
  timeouts do not establish a hard bound for a non-cooperative provider or OS.
- Real handler multiplicity, OS scheduling, malformed/partial HTTP messages,
  arbitrary context values, greater retry chains, and process crashes during
  resource shutdown are outside these small domains. `close` safety is checked;
  no fairness or eventual-shutdown property is claimed. Blocking forever on an
  external operation is not ruled out.
- Recovery's single successful result policy is deliberately distinct from
  all-error lineage validation. The lineage model does not prove that a group
  containing a COMPLETE record has a valid chronological error chain.
- These are manually reviewed abstractions with mutation controls and actual
  regression mappings, not machine-checked source refinement. The ordinary
  checker and separate formal-only checker remain mandatory; neither replaces
  the other. Raw TLC and regression execution outputs belong to unique private
  check directories and are not model source files.
