-------------------------- MODULE StorageValueCall -------------------------
EXTENDS Naturals, TLC
CONSTANT EraseCompletedWrite
VARIABLES phase, effect, admitted, driver, actualWrites, calls,
          completionAttempted, report
vars == <<phase, effect, admitted, driver, actualWrites, calls,
          completionAttempted, report>>
FailureReport == IF effect = "Write" /\ ~EraseCompletedWrite
                 THEN "CompletedUnavailable" ELSE "ValidationError"
Init == /\ phase = "Input"
        /\ effect \in {"Read", "Write"}
        /\ admitted = FALSE /\ driver = "NotStarted" /\ actualWrites = 0
        /\ calls = 0 /\ completionAttempted = FALSE /\ report = "None"
Admit(valid) ==
    /\ phase = "Input" /\ valid \in BOOLEAN
    /\ phase' = IF valid THEN "Executing" ELSE "Failed"
    /\ admitted' = valid
    /\ report' = IF valid THEN "None" ELSE "InputRejected"
    /\ UNCHANGED <<effect, driver, actualWrites, calls, completionAttempted>>
Invoke ==
    /\ admitted /\ driver = "NotStarted" /\ report = "None"
    /\ driver' = "Running" /\ calls' = calls + 1
    /\ UNCHANGED <<phase, effect, admitted, actualWrites, completionAttempted, report>>
DriverReturns(ok, writes) ==
    /\ driver = "Running" /\ ok \in BOOLEAN /\ writes \in 0..1
    /\ (effect = "Read" => writes = 0)
    /\ driver' = IF ok THEN "Succeeded" ELSE "Errored"
    /\ actualWrites' = writes
    /\ report' = IF ok THEN "None" ELSE "Unknown"
    /\ UNCHANGED <<phase, effect, admitted, calls, completionAttempted>>
BeginResult ==
    /\ driver = "Succeeded" /\ ~completionAttempted /\ report = "None"
    /\ completionAttempted' = TRUE
    /\ report' = IF phase = "Executing" THEN "None" ELSE FailureReport
    /\ phase' = IF phase = "Executing" THEN "Result" ELSE "Failed"
    /\ UNCHANGED <<effect, admitted, driver, actualWrites, calls>>
FinishResult(valid) ==
    /\ driver = "Succeeded" /\ completionAttempted /\ report = "None"
    /\ valid \in BOOLEAN
    /\ report' = IF valid /\ phase = "Result" THEN "Success" ELSE FailureReport
    /\ phase' = "Closed"
    /\ UNCHANGED <<effect, admitted, driver, actualWrites, calls, completionAttempted>>
CloseValidator ==
    /\ phase \in {"Input", "Executing", "Result", "Failed"}
    /\ phase' = "Closed"
    /\ UNCHANGED <<effect, admitted, driver, actualWrites, calls, completionAttempted, report>>
Next == \/ \E valid \in BOOLEAN: Admit(valid) \/ FinishResult(valid)
        \/ Invoke \/ BeginResult \/ CloseValidator
        \/ \E ok \in BOOLEAN, writes \in 0..1: DriverReturns(ok, writes)
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"Input", "Executing", "Result", "Failed", "Closed"}
          /\ effect \in {"Read", "Write"} /\ admitted \in BOOLEAN
          /\ driver \in {"NotStarted", "Running", "Succeeded", "Errored"}
          /\ actualWrites \in 0..1 /\ calls \in 0..1 /\ completionAttempted \in BOOLEAN
          /\ report \in {"None", "InputRejected", "Unknown", "Success", "CompletedUnavailable", "ValidationError"}
InvokeRequiresAdmission == calls > 0 => admitted
NoReplay == calls <= 1
NoErasedWriteCompletion == effect = "Write" /\ driver = "Succeeded"
                          /\ report \in {"CompletedUnavailable", "ValidationError"}
                          => report = "CompletedUnavailable"
CompletionRequiresDriverSuccess == report \in {"Success", "CompletedUnavailable"} => driver = "Succeeded"
RejectedInputHasNoEffect == report = "InputRejected" => calls = 0 /\ actualWrites = 0
=============================================================================
