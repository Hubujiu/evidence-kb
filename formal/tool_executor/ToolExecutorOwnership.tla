----------------------- MODULE ToolExecutorOwnership -----------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS LegacyStop, IgnorePublicHint, ReplayTerminal
VARIABLES pc, readOnly, hint, calls, effects, planned, elapsed, failure,
          explicitRetry, delay, outcome, frozenCalls, unknownAt, hintViolation,
          observer, terminalReplay
vars == <<pc, readOnly, hint, calls, effects, planned, elapsed, failure,
          explicitRetry, delay, outcome, frozenCalls, unknownAt, hintViolation,
          observer, terminalReplay>>
MaxAttempts == 3
MaxWait == 4
Deadline == 8
Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b
Init ==
    /\ pc = "absent" /\ readOnly \in BOOLEAN /\ hint \in {0, 2, 8}
    /\ calls = 0 /\ effects = 0 /\ planned = 0 /\ elapsed = 0
    /\ failure = "none" /\ explicitRetry = FALSE /\ delay = 0
    /\ outcome = "none" /\ frozenCalls = 0 /\ unknownAt = 4
    /\ hintViolation = FALSE /\ observer = "none" /\ terminalReplay = FALSE
Claim ==
    /\ pc = "absent" /\ pc' = "ready"
    /\ UNCHANGED <<readOnly, hint, calls, effects, planned, elapsed, failure,
          explicitRetry, delay, outcome, frozenCalls, unknownAt, hintViolation,
          observer, terminalReplay>>
Invoke(effect) ==
    /\ pc = "ready" /\ elapsed < Deadline /\ calls < MaxAttempts
    /\ pc' = "running" /\ calls' = calls + 1 /\ effects' = effects + effect
    /\ UNCHANGED <<readOnly, hint, planned, elapsed, failure, explicitRetry,
          delay, outcome, frozenCalls, unknownAt, hintViolation, observer, terminalReplay>>
Finish(value) ==
    /\ pc = "running"
    /\ pc' = "terminal" /\ outcome' = IF elapsed >= Deadline THEN "ERROR" ELSE value
    /\ frozenCalls' = calls
    /\ UNCHANGED <<readOnly, hint, calls, effects, planned, elapsed, failure,
          explicitRetry, delay, unknownAt, hintViolation, observer, terminalReplay>>
Fail(kind, retry) ==
    /\ pc = "running" /\ pc' = "failed"
    /\ failure' = kind /\ explicitRetry' = retry
    /\ unknownAt' = IF kind = "unknown" THEN Min(unknownAt, calls) ELSE unknownAt
    /\ UNCHANGED <<readOnly, hint, calls, effects, planned, elapsed,
          delay, outcome, frozenCalls, hintViolation, observer, terminalReplay>>
Backoff == IF calls = 1 THEN 1 ELSE 2
WaitDelay == Max(Backoff, IF IgnorePublicHint THEN 0 ELSE hint)
LibraryRetry == explicitRetry \/ (readOnly /\ failure = "transient")
OwnedRetry == LegacyStop \/ (readOnly /\ failure = "transient")
CanRetry == LibraryRetry /\ OwnedRetry /\ calls < MaxAttempts /\
            planned + WaitDelay <= MaxWait /\ elapsed + WaitDelay < Deadline
Assess ==
    /\ pc = "failed"
    /\ pc' = IF CanRetry THEN "waiting" ELSE "terminal"
    /\ planned' = IF CanRetry THEN planned + WaitDelay ELSE planned
    /\ delay' = IF CanRetry THEN WaitDelay ELSE delay
    /\ hintViolation' = (hintViolation \/ (CanRetry /\ WaitDelay < hint))
    /\ outcome' = IF CanRetry THEN outcome ELSE IF failure = "unknown" THEN "UNKNOWN" ELSE "ERROR"
    /\ frozenCalls' = IF CanRetry THEN frozenCalls ELSE calls
    /\ UNCHANGED <<readOnly, hint, calls, effects, elapsed, failure,
          explicitRetry, unknownAt, observer, terminalReplay>>
Wake ==
    /\ pc = "waiting" /\ pc' = "ready"
    /\ elapsed' = Min(Deadline, elapsed + delay)
    /\ UNCHANGED <<readOnly, hint, calls, effects, planned, failure,
          explicitRetry, delay, outcome, frozenCalls, unknownAt, hintViolation,
          observer, terminalReplay>>
Expire ==
    /\ pc = "ready" /\ elapsed = Deadline
    /\ pc' = "terminal" /\ outcome' = "ERROR" /\ frozenCalls' = calls
    /\ UNCHANGED <<readOnly, hint, calls, effects, planned, elapsed, failure,
          explicitRetry, delay, unknownAt, hintViolation, observer, terminalReplay>>
Interrupt ==
    /\ pc \in {"ready", "running", "waiting", "failed"}
    /\ pc' = "terminal" /\ outcome' = "UNKNOWN" /\ frozenCalls' = calls
    /\ unknownAt' = Min(unknownAt, calls)
    /\ UNCHANGED <<readOnly, hint, calls, effects, planned, elapsed, failure,
          explicitRetry, delay, hintViolation, observer, terminalReplay>>
Clock ==
    /\ pc \notin {"absent", "terminal"} /\ elapsed < Deadline
    /\ elapsed' = elapsed + 1
    /\ UNCHANGED <<pc, readOnly, hint, calls, effects, planned, failure,
          explicitRetry, delay, outcome, frozenCalls, unknownAt, hintViolation,
          observer, terminalReplay>>
Observe(match) ==
    /\ pc # "absent" /\ observer = "none"
    /\ observer' = IF ~match THEN "mismatch" ELSE IF pc = "terminal" THEN "reused" ELSE "inprogress"
    /\ pc' = IF match /\ pc = "terminal" /\ ReplayTerminal THEN "ready" ELSE pc
    /\ terminalReplay' = (terminalReplay \/ (match /\ pc = "terminal" /\ ReplayTerminal))
    /\ UNCHANGED <<readOnly, hint, calls, effects, planned, elapsed, failure,
          explicitRetry, delay, outcome, frozenCalls, unknownAt, hintViolation>>
Next == Claim \/ (\E effect \in 0..1 : Invoke(effect)) \/
        (\E value \in {"COMPLETE", "EMPTY"} : Finish(value)) \/
        (\E kind \in {"transient", "permanent", "unknown"}, retry \in BOOLEAN : Fail(kind, retry)) \/
        Assess \/ Wake \/ Expire \/ Interrupt \/ Clock \/ (\E match \in BOOLEAN : Observe(match))
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ pc \in {"absent", "ready", "running", "failed", "waiting", "terminal"}
    /\ readOnly \in BOOLEAN /\ hint \in {0, 2, 8}
    /\ calls \in 0..3 /\ effects \in 0..3 /\ planned \in 0..4 /\ elapsed \in 0..8
    /\ failure \in {"none", "transient", "permanent", "unknown"}
    /\ explicitRetry \in BOOLEAN /\ delay \in 0..8
    /\ outcome \in {"none", "COMPLETE", "EMPTY", "ERROR", "UNKNOWN"}
    /\ frozenCalls \in 0..3 /\ unknownAt \in 0..4 /\ hintViolation \in BOOLEAN
    /\ observer \in {"none", "mismatch", "inprogress", "reused"} /\ terminalReplay \in BOOLEAN
NoMutatingReplay == ~readOnly => calls <= 1
NoUnknownReplay == calls <= unknownAt
RetryAfterRespected == ~hintViolation
TerminalDoesNotExecute == ~terminalReplay \/ calls = frozenCalls
ReceiptKnowledge == pc = "terminal" => outcome # "none" /\ frozenCalls = calls
ActualAttemptBound == effects <= calls /\ calls <= MaxAttempts
WaitBudget == planned <= MaxWait
=============================================================================
