# Native storage codec session table

This models the repository-owned native handle table, separately from the existing one-call `StorageValueCall` model and Python database worker handles. No production bug has been confirmed in this slice.

Reference first: Rust's standard `Mutex` documents RAII-guarded exclusive data access and poison errors. Slotmap documents the stale-key/ABA requirement: a removed key must not silently address a later object. The current implementation achieves non-reuse using checked monotonically increasing u64 IDs rather than introducing a second handle allocator dependency.

- https://doc.rust-lang.org/std/sync/struct.Mutex.html
- https://docs.rs/slotmap/latest/slotmap/

The model has three total allocations, independent one-slot and two-slot profiles, Input/Executing/Result/Values/Failed per-handle phases, explicit expiry, close and consuming finish. The global mutex is an atomic primitive. Age records crossing the idle threshold; Sweep abstracts the next dispatch's cleanup prelude, not an autonomous background timer. Malformed request syntax and codec values are separate concrete guards. Finishing consumes a handle even when the requested phase or encoded result is invalid. Closing a missing handle is idempotent.

Seeded models remove capacity, ID non-reuse, result admission or consuming finish. Witnesses establish values-only consumption, simultaneous handles, released handles and failed live handles. These are reachability evidence and unsafe mutants, not production bug reports.

A native Rust test uses the real dispatch path for 64-slot capacity, release and new monotonic ID, expired-handle rejection (private clock metadata injected at the 300-second boundary, without sleeping), failure-to-consumed transitions, Values-only use, incorrect result ordering and a full get input/admit/result/finish. The test-only module include does not change production behavior. Mandatory semantic lint rejects refreshed-hash mutants.

Remaining boundaries: codec payload parsing, backend execution, external side effects, process loss and OS resources are not proved by this table model. The real u64 exhaustion path is checked source behavior, not three-allocation model equivalence. No fairness or unconditional eventual cleanup is claimed.
