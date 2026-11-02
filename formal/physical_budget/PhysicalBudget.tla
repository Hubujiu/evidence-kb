------------------------- MODULE PhysicalBudget -------------------------
EXTENDS Integers, FiniteSets, TLC

CONSTANTS RequestCount, Reservation, Cap, RetrySettlement, SettlementOutcomes
Requests == 1..RequestCount
States == {"NONE", "STARTED", "COMPLETE", "ERROR", "UNKNOWN"}
CommitResults == {"ACK", "ROLLBACK", "LOST_COMMITTED", "LOST_ROLLED_BACK"}
Persisted(result) == result \in {"ACK", "LOST_COMMITTED"}
Failure(result) == IF result = "ROLLBACK" THEN "Rollback" ELSE "CommitUnknown"

VARIABLES phase, current, ledger, usage, receipt, sends, reservedAck,
          settlements, observed, observedUsage, commitError, reportedError,
          settlementReceipt, crashes
vars == <<phase, current, ledger, usage, receipt, sends, reservedAck,
          settlements, observed, observedUsage, commitError, reportedError,
          settlementReceipt, crashes>>

Charge(r) == IF ledger[r] = "NONE" THEN 0
             ELSE IF usage[r] = -1 THEN Reservation ELSE usage[r]
RECURSIVE SumCharge(_)
SumCharge(n) == IF n = 0 THEN 0 ELSE Charge(n) + SumCharge(n - 1)
Unresolved == \E r \in Requests: ledger[r] \in {"STARTED", "UNKNOWN"}

Init == /\ phase = "Idle" /\ current = 0 /\ crashes = 0
        /\ ledger = [r \in Requests |-> "NONE"]
        /\ receipt = [r \in Requests |-> "NONE"]
        /\ usage = [r \in Requests |-> -1]
        /\ sends = [r \in Requests |-> 0]
        /\ settlements = [r \in Requests |-> 0]
        /\ reservedAck = [r \in Requests |-> FALSE]
        /\ observed = "NONE" /\ observedUsage = -1
        /\ commitError = "NONE" /\ reportedError = "NONE"
        /\ settlementReceipt = "NONE"

BeginReceipt == /\ phase = "Idle" /\ current < RequestCount
                /\ IF current = 0 THEN TRUE ELSE receipt[current] = "ERROR"
                /\ ~Unresolved /\ SumCharge(RequestCount) + Reservation <= Cap
                /\ current' = current + 1 /\ phase' = "Reserve"
                /\ receipt' = [receipt EXCEPT ![current + 1] = "STARTED"]
                /\ observed' = "NONE" /\ observedUsage' = -1
                /\ commitError' = "NONE" /\ reportedError' = "NONE"
                /\ settlementReceipt' = "NONE"
                /\ UNCHANGED <<ledger, usage, sends, reservedAck, settlements, crashes>>

Reserve(result) == /\ phase = "Reserve" /\ result \in CommitResults
                   /\ ledger' = IF Persisted(result)
                                THEN [ledger EXCEPT ![current] = "STARTED"] ELSE ledger
                   /\ reservedAck' = [reservedAck EXCEPT ![current] = (result = "ACK")]
                   /\ phase' = IF result = "ACK" THEN "Ready" ELSE "Failed"
                   /\ reportedError' = IF result = "ACK" THEN "NONE" ELSE Failure(result)
                   /\ UNCHANGED <<usage, receipt, sends, settlements, observed, observedUsage,
                                  commitError, settlementReceipt, current, crashes>>

Send == /\ phase = "Ready" /\ reservedAck[current] /\ ledger[current] = "STARTED"
        /\ sends' = [sends EXCEPT ![current] = @ + 1] /\ phase' = "Sent"
        /\ UNCHANGED <<ledger, usage, receipt, reservedAck, settlements, observed,
                       observedUsage, commitError, reportedError, settlementReceipt, current, crashes>>

Observe(state) == /\ phase = "Sent" /\ state \in {"COMPLETE", "ERROR", "UNKNOWN"}
                  /\ observed' = state /\ observedUsage' = IF state = "UNKNOWN" THEN -1 ELSE 1
                  /\ phase' = "Observed"
                  /\ UNCHANGED <<ledger, usage, receipt, sends, reservedAck, settlements,
                                 commitError, reportedError, settlementReceipt, current, crashes>>

BeginSettlement == /\ phase = "Observed"
                   /\ settlements' = [settlements EXCEPT ![current] = @ + 1]
                   /\ settlementReceipt' = "UNCONFIRMED" /\ phase' = "Commit"
                   /\ UNCHANGED <<ledger, usage, receipt, sends, reservedAck, observed,
                                  observedUsage, commitError, reportedError, current, crashes>>

Finish(result) == /\ phase = "Commit" /\ result \in SettlementOutcomes
                  /\ ledger' = IF Persisted(result)
                               THEN [ledger EXCEPT ![current] = observed] ELSE ledger
                  /\ usage' = IF Persisted(result)
                              THEN [usage EXCEPT ![current] = observedUsage] ELSE usage
                  /\ settlementReceipt' = IF result = "ACK" THEN "COMMITTED" ELSE "UNCONFIRMED"
                  /\ commitError' = IF result = "ACK" THEN "NONE" ELSE Failure(result)
                  /\ reportedError' = IF result = "ACK" THEN "NONE" ELSE Failure(result)
                  /\ phase' = IF result = "ACK" THEN "Settled" ELSE "SettlementFailed"
                  /\ UNCHANGED <<receipt, sends, reservedAck, settlements, observed,
                                 observedUsage, current, crashes>>

HandleSettlementFailure == /\ phase = "SettlementFailed"
                           /\ settlements' = IF RetrySettlement
                                THEN [settlements EXCEPT ![current] = @ + 1] ELSE settlements
                           /\ reportedError' = IF RetrySettlement /\ ledger[current] # "STARTED"
                                                THEN "TerminalRowError" ELSE reportedError
                           /\ phase' = "Failed"
                           /\ UNCHANGED <<ledger, usage, receipt, sends, reservedAck, observed,
                                          observedUsage, commitError, settlementReceipt, current, crashes>>

FinishReceipt == /\ phase \in {"Settled", "Failed"}
                 /\ receipt' = [receipt EXCEPT ![current] = IF phase = "Settled" THEN observed ELSE "UNKNOWN"]
                 /\ phase' = "Idle"
                 /\ UNCHANGED <<ledger, usage, sends, reservedAck, settlements, observed,
                                observedUsage, commitError, reportedError, settlementReceipt, current, crashes>>

Crash == /\ phase # "Idle" /\ phase # "Stopped" /\ crashes = 0
         /\ phase' = "Stopped" /\ crashes' = 1
         /\ UNCHANGED <<current, ledger, usage, receipt, sends, reservedAck, settlements,
                        observed, observedUsage, commitError, reportedError, settlementReceipt>>
Restart == /\ phase = "Stopped" /\ phase' = "Idle"
           /\ UNCHANGED <<current, ledger, usage, receipt, sends, reservedAck, settlements,
                          observed, observedUsage, commitError, reportedError, settlementReceipt, crashes>>

Next == BeginReceipt \/ (\E result \in CommitResults: Reserve(result) \/ Finish(result))
        \/ Send \/ (\E state \in {"COMPLETE", "ERROR", "UNKNOWN"}: Observe(state))
        \/ BeginSettlement \/ HandleSettlementFailure \/ FinishReceipt \/ Crash \/ Restart

TypeOK == /\ phase \in {"Idle", "Reserve", "Ready", "Sent", "Observed", "Commit", "Settled",
                        "SettlementFailed", "Failed", "Stopped"}
          /\ current \in 0..RequestCount /\ crashes \in 0..1
          /\ ledger \in [Requests -> States] /\ receipt \in [Requests -> States]
          /\ usage \in [Requests -> {-1, 1}] /\ observedUsage \in {-1, 1}
          /\ observed \in States /\ sends \in [Requests -> 0..1]
          /\ reservedAck \in [Requests -> BOOLEAN] /\ settlements \in [Requests -> 0..2]
          /\ settlementReceipt \in {"NONE", "UNCONFIRMED", "COMMITTED"}
          /\ commitError \in {"NONE", "Rollback", "CommitUnknown"}
          /\ reportedError \in {"NONE", "Rollback", "CommitUnknown", "TerminalRowError"}
SendRequiresAcknowledgedReservation == \A r \in Requests:
    sends[r] > 0 => reservedAck[r] /\ ledger[r] # "NONE"
NoPhysicalReplay == \A r \in Requests: sends[r] <= 1
SettlementAtMostOnce == \A r \in Requests: settlements[r] <= 1
UnknownChargeProtected == \A r \in Requests:
    ledger[r] \in {"STARTED", "UNKNOWN"} => Charge(r) = Reservation
AdmissionBounded == SumCharge(RequestCount) <= Cap
ConfirmedSettlementIsDurable == settlementReceipt = "COMMITTED" =>
    ledger[current] = observed /\ usage[current] = observedUsage
OriginalCommitFailurePreserved == commitError # "NONE" => reportedError = commitError
Spec == Init /\ [][Next]_vars
=============================================================================
