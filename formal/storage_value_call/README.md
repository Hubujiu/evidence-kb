# Value-call validation and known driver completion

This stage adds a model and implementation-binding contracts, not a production
bug fix. The seeded negative behavior is hypothetical; the current implementation
already preserves known completion when validating or publishing a write result
fails. It does not convert an arbitrary driver exception into success.

## Mature implementation and knowledge boundary

The inspected Rust `PreparedOperation.complete` and `Operation.result_failure`
classify post-return write-result errors as `STORAGE_WRITE_RESULT_UNAVAILABLE`
with `outcome=COMPLETED`. `BoundStorage._execute` invokes once, then validates the
returned value; `PreparedCall.complete` preserves that classification even if the
native validator session was closed or expired before its result phase. This is
the existing ownership boundary, not a second driver or retry wrapper.

The [FoundationDB unknown-result guidance](https://apple.github.io/foundationdb/developer-guide.html#transactions-with-unknown-results)
was reviewed before the storage work: a failed observation does not authorize
blind replay. The model deliberately distinguishes the cases: a driver exception
may follow an actual write and remains unknown, whereas a successful driver return
followed only by result-validation failure is known completed work. No FoundationDB
idempotency mechanism is assumed to exist in this storage API.

## Model binding

| Model | Source |
| --- | --- |
| `phase`, `Admit` | Rust `service::Phase`, `dispatch` input/admit validation |
| `effect` | Rust `PreparedOperation.operation.effect`, fixed before invocation |
| `Invoke`, `calls` | Python `BoundStorage._execute` single `invoke()` |
| `driver`, `actualWrites`, `DriverReturns` | Driver return evidence versus possible actual effect; an error does not force actual writes to zero |
| `BeginResult`, `FinishResult` | `PreparedCall.complete`, Rust result/feed/finish, `PreparedOperation.complete` |
| `CloseValidator` | Rust close/session expiry; does not cancel or undo driver work |
| `FailureReport`, `report` | Rust `Operation.result_failure` plus Python `result_failure` fallback |

The new real-native regression closes the Rust validator just before `result`,
after a real local object write returned. It verifies the object remains readable,
the public exception says `COMPLETED`, and the driver was invoked once. Existing
Rust/storage contracts still cover invalid payload/result values and publication
failure. Source fingerprints bind the exact reviewed declarations; hashes and
these tests are not a formal refinement proof.

## Bounds and exclusions

One operation/session, fixed read/write effect, at most one driver invocation and
one actual mutation. A successful write-capable method can be a no-op: known
completion is not evidence of changed rows or bytes. Input/result validity and validator loss are abstract events.
The validator may disappear before or after invocation; that does not change the
actual effect. The model does not cover Values-only sessions, full codec events,
size/type arithmetic, 64-session capacity, exact TTL clock behavior, complete
method catalogue, S3 transport, SQL revision/CAS semantics or arbitrary drivers.
No liveness/fairness assumption forces a blocked callback to return. Terminal
stuttering is allowed with `CHECK_DEADLOCK FALSE`.

`StorageValueCallNegative.cfg` changes only `EraseCompletedWrite`; it must exit 12
on `NoErasedWriteCompletion`. It is a detection control, not evidence of a current
production defect. Positive and negative logs plus machine-readable input hashes
are retained in `evidence/`. The parent owns full-project validation and PRs.
All implementation checks are offline and use synthetic faults; new remote
model/embedding/judge calls: zero. Partial coverage is recorded in the registry.

Initial local evidence (before no-op correction): value-call TLC 49 generated / 36 distinct states, negative
exit 12; whole formal runner PASS; 33 focused storage/ownership contracts and 18
formal infrastructure contracts PASS; Ruff PASS. The focused output is in
`evidence/python-focused.log`; it is not the full project gate.

A review correction allows zero actual mutations even after successful write-method
return. The original run remains historical evidence; `*-no-op-*` logs record the
corrected model and a real SQLite missing-key delete regression.

Current corrected outcomes: TLC 60 generated / 43 distinct states; intentional
negative exit 12; formal runner PASS; 41 focused contracts PASS including the new
missing-key SQLite delete. Its driver returns `False` (zero deletions), yet a
subsequent malformed result is still correctly reported as method `COMPLETED`.
The intermediate 15-test diagnostic imported another test class directly; final
code imports its module instead, so unified discovery does not duplicate cases.
`python-no-op-focused.log` is the final focused test result.
