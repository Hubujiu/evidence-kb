------------------------- MODULE RemoteRetryFormat -------------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC
CONSTANTS RetryUnknown, ReuseIdentity, ThirdFormat, ClampHint
VARIABLES pc, fmt, transport, actualCalls, records, elapsed, waited, delay, required, reason, lastKind, unknownAt, cancelled
vars == <<pc, fmt, transport, actualCalls, records, elapsed, waited, delay, required, reason, lastKind, unknownAt, cancelled>>
Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b
Deadline == 5
WaitCap == 2
FormatCap == IF ThirdFormat THEN 3 ELSE 2
MaxCalls == 6
Init ==
    /\ pc = "ready" /\ fmt = 1 /\ transport = 0 /\ actualCalls = 0
    /\ records = <<>> /\ elapsed = 0 /\ waited = 0 /\ delay = 0 /\ required = 0
    /\ reason = "initial" /\ lastKind = "none" /\ unknownAt = 7 /\ cancelled = FALSE
Dispatch ==
    /\ pc = "ready" /\ ~cancelled /\ elapsed < Deadline /\ actualCalls < MaxCalls
    /\ pc' = "running"
    /\ transport' = transport + 1
    /\ actualCalls' = actualCalls + 1
    /\ records' = Append(records, [id |-> IF ReuseIdentity /\ actualCalls > 0 THEN records[actualCalls].id ELSE actualCalls + 1, parent |-> IF actualCalls = 0 THEN 0 ELSE records[actualCalls].id, outcome |-> "STARTED", effect |-> 0, format |-> fmt, transport |-> transport + 1, reason |-> reason, delay |-> delay, required |-> required, waited |-> waited])
    /\ UNCHANGED <<fmt, elapsed, waited, delay, required, reason, lastKind, unknownAt, cancelled>>
Effect ==
    /\ pc = "running" /\ records[actualCalls].effect = 0
    /\ records' = [records EXCEPT ![actualCalls].effect = 1]
    /\ UNCHANGED <<pc, fmt, transport, actualCalls, elapsed, waited, delay, required, reason, lastKind, unknownAt, cancelled>>
Observe(kind) ==
    /\ pc = "running" /\ (kind # "not_sent" \/ records[actualCalls].effect = 0)
    /\ pc' = "assess"
    /\ lastKind' = kind
    /\ unknownAt' = IF kind = "unknown" THEN Min(unknownAt, actualCalls) ELSE unknownAt
    /\ records' = [records EXCEPT ![actualCalls].outcome = IF kind = "unknown" THEN "UNKNOWN" ELSE IF kind \in {"not_sent", "retry_http", "permanent_http"} THEN "ERROR" ELSE "COMPLETE"]
    /\ UNCHANGED <<fmt, transport, actualCalls, elapsed, waited, delay, required, reason, cancelled>>
Retryable == lastKind \in {"not_sent", "retry_http"} \/ (RetryUnknown /\ lastKind = "unknown")
Needed(hint) == Max(1, hint)
Chosen(hint) == IF ClampHint THEN 1 ELSE Needed(hint)
CanRetry(hint) == Retryable /\ transport < 2 /\ waited + Chosen(hint) <= WaitCap /\ elapsed + Chosen(hint) < Deadline
AssessRetry(hint) ==
    /\ pc = "assess" /\ lastKind \in {"not_sent", "unknown", "retry_http", "permanent_http"}
    /\ (lastKind = "retry_http" \/ hint = 0)
    /\ pc' = IF CanRetry(hint) THEN "waiting" ELSE "terminal"
    /\ waited' = IF CanRetry(hint) THEN waited + Chosen(hint) ELSE waited
    /\ delay' = IF CanRetry(hint) THEN Chosen(hint) ELSE delay
    /\ required' = IF CanRetry(hint) THEN Needed(hint) ELSE required
    /\ reason' = IF CanRetry(hint) THEN lastKind ELSE reason
    /\ UNCHANGED <<fmt, transport, actualCalls, records, elapsed, lastKind, unknownAt, cancelled>>
Wake ==
    /\ pc = "waiting"
    /\ pc' = "ready"
    /\ elapsed' = Min(Deadline, elapsed + delay)
    /\ UNCHANGED <<fmt, transport, actualCalls, records, waited, delay, required, reason, lastKind, unknownAt, cancelled>>
Correct ==
    /\ pc = "assess" /\ lastKind = "malformed_completion" /\ fmt < FormatCap
    /\ pc' = "ready"
    /\ fmt' = fmt + 1
    /\ transport' = 0
    /\ waited' = 0
    /\ delay' = 0
    /\ required' = 0
    /\ reason' = "format_correction"
    /\ UNCHANGED <<actualCalls, records, elapsed, lastKind, unknownAt, cancelled>>
Finish ==
    /\ pc = "assess" /\ (lastKind \in {"valid", "invalid_envelope", "terminal_contract"} \/ (lastKind = "malformed_completion" /\ fmt = FormatCap))
    /\ pc' = "terminal"
    /\ UNCHANGED <<fmt, transport, actualCalls, records, elapsed, waited, delay, required, reason, lastKind, unknownAt, cancelled>>
Refuse ==
    /\ pc = "ready" /\ (cancelled \/ elapsed = Deadline)
    /\ pc' = "terminal"
    /\ UNCHANGED <<fmt, transport, actualCalls, records, elapsed, waited, delay, required, reason, lastKind, unknownAt, cancelled>>
Cancel ==
    /\ pc # "terminal" /\ ~cancelled
    /\ cancelled' = TRUE
    /\ UNCHANGED <<pc, fmt, transport, actualCalls, records, elapsed, waited, delay, required, reason, lastKind, unknownAt>>
Clock ==
    /\ pc # "terminal" /\ elapsed < Deadline
    /\ elapsed' = elapsed + 1
    /\ UNCHANGED <<pc, fmt, transport, actualCalls, records, waited, delay, required, reason, lastKind, unknownAt, cancelled>>
Next == Dispatch \/ Effect \/ Wake \/ Correct \/ Finish \/ Refuse \/ Cancel \/ Clock
    \/ (\E kind \in {"not_sent", "unknown", "retry_http", "permanent_http", "valid", "malformed_completion", "invalid_envelope", "terminal_contract"}: Observe(kind))
    \/ (\E hint \in {0, 1, 3}: AssessRetry(hint))
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ pc \in {"ready", "running", "assess", "waiting", "terminal"}
    /\ fmt \in 1..3 /\ transport \in 0..2 /\ actualCalls \in 0..MaxCalls
    /\ elapsed \in 0..Deadline /\ waited \in 0..WaitCap /\ delay \in 0..3 /\ required \in 0..3
    /\ unknownAt \in 0..7 /\ cancelled \in BOOLEAN
    /\ Len(records) = actualCalls
ActualAttemptAccounting == Len(records) = actualCalls
NoUnknownReplay == actualCalls <= unknownAt
IdentityOwnership == Cardinality({records[i].id: i \in 1..actualCalls}) = actualCalls
RetryLinks == \A i \in 2..actualCalls: records[i].parent = records[i-1].id /\ records[i].reason # "initial"
FormatLimit == \A i \in 1..actualCalls: records[i].format <= 2
TransportLimit == \A i \in 1..actualCalls: records[i].transport <= 2
WaitAdmission == \A i \in 1..actualCalls: records[i].waited <= WaitCap /\ records[i].delay >= records[i].required
KnownNotSent == lastKind = "not_sent" /\ pc = "assess" => records[actualCalls].effect = 0
NoPendingReplay == \A i \in 1..(actualCalls-1): records[i].outcome \notin {"STARTED", "UNKNOWN"}
SuccessUnreachable == ~(pc = "terminal" /\ lastKind = "valid" /\ actualCalls = 4 /\ fmt = 2)
=============================================================================
