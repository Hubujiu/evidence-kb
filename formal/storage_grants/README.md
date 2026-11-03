# Storage registration / grant ownership

## Mature reference and confirmed failures

[slotmap 1.1.1](https://docs.rs/slotmap/1.1.1/slotmap/) documents versioned keys: deletion must not make an old key valid when its slot is reused (ABA). We reuse the project's existing opaque grant object identity rather than introduce a second token framework. The registry also pins the actual captured entry across the external ownership callback, following the same identity-fencing principle. PR40 close/disposal state handling is retained unchanged.

Two real boundaries failed on the immutable PR40 base:

1. Both StorageService and DatabaseService permit the same explicit registration string under a different owner identity. Disposal removes its grants, but an already-bound handle previously checked only route + string. After replacement it could execute the **old** cache implementation, or write the old SQLite dataset while leasing the new route. Four baseline contracts fail as intended, including actual native JSON KV validation and actual SQLite writes.
2. Owner-lease context entry runs outside the registry lock. That callback can dispose and replace the route before admission increments active. The original second check silently selected the new entry, so the ownership context and selected entry belonged to different owners. Deterministic callback barriers exercise both registries without timing sleeps.

The fix preserves deliberate cross-owner registration reuse. Bound bind/use admissions now validate their exact original grant before and after external ownership entry. All lease admissions, including direct registry leases, require the second lookup to be the identical entry captured before that callback. Rejection occurs before increment/yield, and successful calls retain original try/finally active release.

## Model correspondence

StorageGrants abstracts one route, two owner generations reusing the same public registration, one pending call, and atomic locked admission/release. `Capture` corresponds to first locked lookup; `Replace` to disposal + re-registration outside an active lease; `Admit` to the second locked lookup plus increment. `binding=0` is a direct registry lease, not a forged bound grant. Binding 1 remains revoked after replacement; a fresh generation-2 grant is allowed. Active calls prevent replacement. `Invoke` uses the stored bound implementation, which is why merely leasing a current route does not repair an old handle.

- Positive: ownership, nonrevival, implementation identity, no effects after rejection.
- GrantNegative: removes only grant validation, reaches RevokedGrantCannotRun.
- EntryNegative: removes only captured-entry identity, reaches OwnerContextMatches for a direct registry lease.
- NewWitness, RevokedWitness, SwapWitness are **reachability observers**, not seeded production defects: designated anti-invariants must fail on the correct implementation. They establish fresh-owner success, revoked bound refusal and callback replacement refusal respectively.

The initial positive model explored 30 distinct states. This small explicit boundary is not the entire storage protocol. It excludes grant UUID collision, hostile Python object mutation, backend transactional correctness, cross-process persistence, repeated/unbounded owners, prepare/seal, multiple simultaneous calls, and progress/fairness. Existing PR40 lifecycle and all integration gates remain mandatory.

## Lint and evidence

`scripts/storage_grant_checks.py` is called by mandatory registry lint. It independently reviews the lock/ownership/recheck/increment/yield/finally skeleton, opaque grant checks, and forwarding at bind and bound lease. Mutations of guards, object identity, counters or forwarding must fail even without source hashes. This deliberately strict AST shape requires explicit review when changed; it is not a compiler refinement proof.

Raw baseline/model/manual run evidence is retained separately during development; exact final formal inputs and raw TLC logs are mandatory CI artifacts. Initial baseline test source also had a lint style issue; that was corrected without changing assertions. No retries, sleeps, permission changes, production merge or deployment are used to obtain a pass.
