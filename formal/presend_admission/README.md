# Admission must be current after receipt persistence

This finite safety model covers the interval between provider receipt admission and HTTP/SDK handoff. It is not a full provider, budget, SQL, filesystem, network or deadline-enforcement proof.

## Dependency and mature precedents

This patch depends on PR #31's exact commit `7d4b2f643592fc5188b402968839242ba42511d0`, which owns canonical `model_errors.error_details` / `model_error` normalization. It does not duplicate the older `llm_contract` normalizer. The only normalization extension is carrying `operation_id`, `admission_receipt_id` and `known_zero_send` through its existing structured fields.

- [HTTPX timeout documentation](https://www.python-httpx.org/advanced/timeouts/) distinguishes connection, read, write and pool timeouts. Passing a previously computed duration is not an absolute-deadline check, and these per-phase timeouts do not establish a whole-request wall-clock bound.
- Existing `agent_runtime.serialized_send` already rechecks cancellation/deadline after waiting for its send lock and clamps request timeouts. The transport fix follows the same post-wait validation principle, without adding another lock or imposing that delivery-specific client wrapper on every SDK client.
- [SQLite transaction-state guidance](https://www.sqlite.org/c3ref/get_autocommit.html) explains why an error alone does not establish rollback. PR #31 already separates storage uncertainty from known model-send facts; this change preserves that separation instead of replacing a storage error with the cancellation/deadline error.

No external source code was copied and no dependency was added.

## Reproduced bug and scoped repair

`_Transport._send_attempt` formerly checked cancellation and calculated `remaining` before acquiring the stats lock and running `RequestReceipts.begin`. A delay in receipt I/O, database admission or the separate file-receipt guard fix could exhaust the deadline or observe cancellation. The old code still incremented issued/attempt accounting and handed the request to the client with the old timeout.

The issue is **pre-existing** on main `8160c051e63952278c5f8a80d1b024ebbe3cea17`, demonstrated by pausing actual receipt persistence. The file receipt guard did not create the missing check; its bounded wait made the gap more visible. Its independent fix is not bundled here.

Controlled Python reproduction used fake monotonic time, thread Events, the actual `_Transport._post`/HTTPX dispatch path and `MockTransport`:

- Initial time 100, deadline 110, resume at 111: legacy sent once with timeout 10.
- Cancellation while persistence waited: legacy sent once with cancellation already set.
- Resume at 105: legacy sent with timeout 10 instead of 5.

The new code rechecks immediately after successful `begin`, while still inside the existing stats lock, **before** incrementing `_issued_requests` or exposing `metadata['physical_request_id']`. Successful admission recomputes remaining time. No new lock, retry loop or HTTP wrapper is introduced.

On refusal, the newly created receipt is finalized exactly once as ERROR with known-unsent facts. `admission_receipt_id` retains its identity without masquerading as a sent physical attempt. Existing Luna and Google `_post` paths restore attempt counts because no sent physical identity was exposed. The metered transport has not run, so it reserves nothing and there is no new model/embedding/judge charge.

If ERROR publication fails, the **same storage exception object**, code, operation ID and cause are re-raised directly, annotated with `model_sent=False`, `known_zero_send=True`, `preflight_code` and `admission_receipt_id`. It is not replaced by a deadline error or a string note. Across the JSON boundary, canonical normalization preserves those structured facts; arbitrary Python exception objects are not serialized.

Rollback and physical-write acknowledgement loss leave `.pending` intact. A fully completed terminal publication followed by acknowledgement loss may already have removed pending; the code does not claim otherwise. Its durable ERROR remains and a bare replay is rejected. Neither outcome triggers a second finalization, blind replay or automatic deletion. Existing UNKNOWN reservations and unrelated ledger entries are not changed.

## Linearization and deliberate limits

The checked admission boundary is the final post-`begin` validation before handing control to `_request_response`. `_issued_requests` records that handoff admission, not proof that bytes reached the provider. Once handoff begins, existing transport/SDK outcome and budget semantics remain responsible for known-unsent versus UNKNOWN outcomes.

This patch does not make cancellation, monotonic time and an eventual socket write one atomic operation. Cancellation after the final check, thread scheduling, later SDK request construction, connection-pool waits or external HTTP behavior are outside this proof. Delivery's existing `serialized_send` supplies an additional later queue check where installed. Direct HTTPX clients do not gain a universal absolute wall-clock or interrupt-on-cancel guarantee.

## Model and source mapping

One physical identity, deadline 2, monotonic admission time 0 through 3, cancellation during the wait, and four finalization outcomes are explored:

| Model action | Implementation |
| --- | --- |
| `Begin`, `Tick`, `Cancel` | successful STARTED receipt creation plus blocking admission/persistence interval |
| `Admit`, `Reject` | `remaining_time()` after `begin`, before issued count and sent identity |
| `Finalize(ACK)` | known-unsent ERROR publication acknowledged |
| `Finalize(ROLLBACK)` | publication fails before persisted mutation; STARTED/pending remains |
| `Finalize(PHYSICAL_ACK_LOST)` | physical ERROR persisted but terminal publication/pending removal unconfirmed |
| `Finalize(TERMINAL_ACK_LOST)` | full terminal publication persisted, acknowledgement lost |
| `ReportFailure` | outer Luna/Google attempt correction; structured failure propagation |
| `Dispatch` | abstract HTTP/SDK handoff and successful response, not a model of network internals |

Time/cancellation changes are confined to the blocking-admission phase. The final validation-to-handoff interval is abstracted as having no further blocking admission. This is an explicit scope boundary, not a claim of real-world atomic socket cancellation. Initial admission refusal, failure of `begin` itself, concurrent receipt writers, multiple retries, late storage commits and power-loss durability are excluded; other models/contracts own those boundaries.

Checked invariants cover current dispatch permission, fresh timeout, zero sends/issued counts after refusal, zero returned attempt accounting, one finalization, preserved storage operation identity and retained partial-publication pending authority. No fairness or recovery-liveness property is claimed.

Pinned TLC 1.7.4, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`, Java 21, one worker:

- `PresendAdmission.cfg`: **70 generated / 67 distinct states**, depth **9**, no violation, exit **0**.
- `PresendAdmissionLegacy.cfg`: stale timeout violates `TimeoutBounded`, depth **5**, exit **12**.
- `PresendAdmissionLegacyDispatch.cfg`: dispatch after cancellation violates `DispatchAuthorized`, depth **5**, exit **12**.

```sh
java -XX:+UseParallelGC -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -cleanup -config PresendAdmission.cfg formal/presend_admission/PresendAdmission.tla
java -XX:+UseParallelGC -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -cleanup -config PresendAdmissionLegacy.cfg formal/presend_admission/PresendAdmission.tla
java -XX:+UseParallelGC -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -cleanup -config PresendAdmissionLegacyDispatch.cfg formal/presend_admission/PresendAdmission.tla
```

The offline unified contracts exercise actual SDK/metered code with synthetic responses, including deadline, cancellation, partial wait, rollback, partial/full lost acknowledgement, raw exception identity and JSON public structured completion with zero upper/turn budgets. `presend_admission_checks.py` checks the post-receipt check's dominance, fresh timeout, one ERROR finalization and original-storage-error exit. Parseable negative mutations and positive annotation/quote changes are required controls. These source-shape checks are not semantic equivalence proofs.

The targeted JSON public tests used explicitly configured `/tmp/evidence-baseline-8160c051-20261102/native/libagent_kernel.so`, built from main with Rust 1.99; Rust sources are unchanged by PR #31 or this patch. This is targeted source-compatible evidence, not a full combined-branch native rebuild. The parent aggregate gate rebuilds the final branch. All new model, embedding and judge calls are zero.


## Canonical registry integration and review anchors

The central registry marks `provider_admission` **partial** only for this finite
post-receipt slice. Existing persistent 403 blocking/checkpoint acknowledgement,
rate/retry/circuit behavior and the broader receipt inventory remain explicit
obligations; the file-receipt guard patch is not part of this candidate.

`source-review-v1.json` records the exact PR31 source review: 11 reviewed old/new
fingerprints and three extracted anchors, with 21 existing bindings verified
unchanged against both dependency and feature snapshots. The pre-send transport
is separately reviewed and bound in both relevant inventory families. Canonical
`model_errors.<module>` binds the `ERROR_FIELDS` tuple as well as the shared
normalizer; the only feature change there adds `operation_id`,
`admission_receipt_id` and `known_zero_send`, leaving pre-existing function ASTs
unchanged. Five read-only budget/status projection anchors are explicitly
**unmodeled**; they do not turn operator review into settlement or erase UNKNOWN.

Both ordinary formal lint and `--formal-only` mandatorily run the pre-send source
checks. A refreshed source fingerprint still cannot admit a removed post-receipt
check or replaced storage exception. Separate contracts show that deleting a
canonical error-field constant changes the module binding even when the
`error_details` function fingerprint stays unchanged.

Integration validation uses the current formal-base native build, whose production
Rust sources are byte-identical to this dependency plus feature; it does not rely
on the older main-baseline binary described in the original targeted evidence.
Full final-branch CI remains separately required. Hash review is not source
refinement, and the finite model establishes no additional cross-family proof.
