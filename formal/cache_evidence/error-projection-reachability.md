# Projection reachability correction

This is a model and validation correction over local `2e03546346b05b5f95a4c2f3f07672db1075cf54`
(remote PR44 `6e2a96b95f7e4ca263a8dc54a168e9f68674035a`). All production modules,
existing actual contract tests, historical evidence, canonical error normalizer,
and `scripts/check_project.py` are unchanged. The mandatory registry lint now
also invokes a deliberately narrow reviewed-shape check. Its contracts reject
old unparenthesized RHS, an Init-only predicate, omitted registration, removal
of both registration and config, and a witness with a fault enabled. Refreshing
Python source hashes does not satisfy these checks.

## Actual results

The original assignment plus the new coded terminal witness exhausts 60 states
with TLC exit 0: the required reachability check correctly reports **FAIL**.
The corrected positive model exhausts 70 states, and all seven terminal witnesses
produce their exact expected invariant violations. The unified additional gate
has 31/31 expected TLC outcomes plus two evidence contracts PASS; 27 targeted
Python contracts PASS. This corrects the earlier misleading coverage claim, not
a newly discovered runtime bug. Ordinary project acceptance remains separate;
no prior full-gate failure or incomplete run is relabeled or discarded.

The live runner and evidence schema are unchanged. Compact historical summaries,
original per-run `SHA256SUMS`, and raw logs are in
[evidence/projection-reachability](evidence/projection-reachability/).
The legacy witness's full input snapshot is retained as `.tla.txt`, not loaded as
an active model. An initial auxiliary helper failed before TLC because its output
parent did not exist; that setup failure is retained and the corrected run uses
a distinct output directory. No model timeout, bound or invariant was relaxed.

## Read-only same-class review

The following is a manual review of current source, not a new TLA parser or
unbounded semantic proof. Candidate searches were followed by reading the whole
actions; normal action conjunctions were not globally parenthesized. Official
TLC remains the execution authority. No other current same-class mistake was
identified in these ten owned models.

| Model and reviewed compound RHS candidates | Finding |
| --- | --- |
| `AuthSession.Validate.authenticated` | Complete Boolean RHS already parenthesized; `owners` / `issued` and read phase changes are independent conjuncts. |
| `StageCache.ReadPayload.delivered` | Complete Boolean RHS already parenthesized; `BeginRead.phase` uses Boolean IF condition, not a Boolean value assignment. |
| `EvidenceContinuation.RejectOther.wrongConsumed` | Complete Boolean OR RHS already parenthesized; the other matches are separate assignments. |
| `ModelReceiptSlots.Settle.confirmed/accepted` | Conjunctions occur inside IF conditions; remaining matches join intentionally independent set updates. |
| `CapacityRefusal.Admit` | Independent phase, slot and dispatch assignments, not one Boolean RHS. |
| `ErrorProjection.Project.privateVisible` | Confirmed mistake corrected by complete RHS parentheses; wire/public fields use IF conditions. Added terminal witnesses. |
| `BatchSealing.Validate.phase`, `CommitFirst.seals` | Conditions belong to IF/THEN/ELSE and EXCEPT expressions; `known`/`usage` are independent assignments. |
| `ImportIntent.PinIntent.intent` | Complete IF value already parenthesized before the separate phase assignment; marker Boolean is within EXCEPT. |
| `TurnReservations.Attempt.reservedAtSend` | Membership is inside EXCEPT; all other matched updates are distinct assignments or IF predicates. |
| `DatabaseReceipts.AbortOrRestart.rejectedStale` | Corrected complete Boolean OR/IF RHS is parenthesized in independent ReceiptDB feature c81c8ba; its old development exit-75 evidence remains there. Other matches are independent updates or IF predicates. |

The budget rows refer to frozen `9424fa850d8c0a2a2cb56559ef511f1f3c21c1cc`, and the
database row to frozen `c81c8ba9c561ca6198d4ba7efe21afdad44af235`. They are read-only
review references, not changes or newly integrated models in PR44. Standard
`reviewed-models/SHA256SUMS` records the actual reviewed bytes for each tree.
