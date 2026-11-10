-------------------------- MODULE PresendAdmission --------------------------
EXTENDS Naturals
CONSTANTS Recheck, Deadline, MaxTime
VARIABLES phase, now, cancelled, receipt, pending, issued, attempts, sent,
          timeout, allowed, preflightCode, errorCode, operationId, knownZero,
          modelSent, finalizations, publication
vars == <<phase, now, cancelled, receipt, pending, issued, attempts, sent,
          timeout, allowed, preflightCode, errorCode, operationId, knownZero,
          modelSent, finalizations, publication>>
Modes == {"ACK", "ROLLBACK", "PHYSICAL_ACK_LOST", "TERMINAL_ACK_LOST"}
Eligible == ~cancelled /\ now < Deadline
Init == /\ phase = "Begin" /\ now = 0 /\ cancelled = FALSE
        /\ receipt = "ABSENT" /\ pending = FALSE
        /\ issued = 0 /\ attempts = 1 /\ sent = 0
        /\ timeout = Deadline /\ allowed = TRUE
        /\ preflightCode = "" /\ errorCode = "" /\ operationId = ""
        /\ knownZero = FALSE /\ modelSent = "UNOBSERVED"
        /\ finalizations = 0 /\ publication = "NONE"
Begin == /\ phase = "Begin" /\ Eligible
         /\ receipt' = "STARTED" /\ pending' = TRUE /\ phase' = "Wait"
         /\ UNCHANGED <<now, cancelled, issued, attempts, sent, timeout, allowed,
                          preflightCode, errorCode, operationId, knownZero, modelSent,
                          finalizations, publication>>
Tick == /\ phase = "Wait" /\ now < MaxTime /\ now' = now + 1
        /\ UNCHANGED <<phase, cancelled, receipt, pending, issued, attempts, sent,
                         timeout, allowed, preflightCode, errorCode, operationId,
                         knownZero, modelSent, finalizations, publication>>
Cancel == /\ phase = "Wait" /\ ~cancelled /\ cancelled' = TRUE
          /\ UNCHANGED <<phase, now, receipt, pending, issued, attempts, sent,
                           timeout, allowed, preflightCode, errorCode, operationId,
                           knownZero, modelSent, finalizations, publication>>
Admit == /\ phase = "Wait" /\ (~Recheck \/ Eligible)
         /\ allowed' = Eligible /\ issued' = 1 /\ phase' = "Dispatch"
         /\ timeout' = IF Recheck THEN Deadline - now ELSE timeout
         /\ UNCHANGED <<now, cancelled, receipt, pending, attempts, sent,
                          preflightCode, errorCode, operationId, knownZero, modelSent,
                          finalizations, publication>>
Reject == /\ phase = "Wait" /\ Recheck /\ ~Eligible
          /\ preflightCode' = IF cancelled THEN "CANCELLED" ELSE "DEADLINE"
          /\ knownZero' = TRUE /\ modelSent' = FALSE /\ phase' = "FinalizeUnsent"
          /\ UNCHANGED <<now, cancelled, receipt, pending, issued, attempts, sent,
                           timeout, allowed, errorCode, operationId, finalizations, publication>>
Finalize(mode) == /\ phase = "FinalizeUnsent" /\ mode \in Modes
                  /\ finalizations' = finalizations + 1 /\ publication' = mode
                  /\ receipt' = IF mode = "ROLLBACK" THEN receipt ELSE "ERROR"
                  /\ pending' = (mode \in {"ROLLBACK", "PHYSICAL_ACK_LOST"})
                  /\ errorCode' = IF mode = "ACK" THEN preflightCode ELSE "COMMIT_UNKNOWN"
                  /\ operationId' = IF mode = "ACK" THEN "" ELSE "receipt-operation"
                  /\ phase' = "ReportFailure"
                  /\ UNCHANGED <<now, cancelled, issued, attempts, sent, timeout,
                                   allowed, preflightCode, knownZero, modelSent>>
ReportFailure == /\ phase = "ReportFailure" /\ attempts' = 0 /\ phase' = "Done"
                 /\ UNCHANGED <<now, cancelled, receipt, pending, issued, sent, timeout,
                                  allowed, preflightCode, errorCode, operationId,
                                  knownZero, modelSent, finalizations, publication>>
Dispatch == /\ phase = "Dispatch" /\ sent' = 1 /\ modelSent' = TRUE
            /\ receipt' = "COMPLETE" /\ pending' = FALSE /\ phase' = "Done"
            /\ UNCHANGED <<now, cancelled, issued, attempts, timeout, allowed,
                             preflightCode, errorCode, operationId, knownZero,
                             finalizations, publication>>
Next == Begin \/ Tick \/ Cancel \/ Admit \/ Reject \/ ReportFailure \/ Dispatch
        \/ (\E mode \in Modes : Finalize(mode))
TypeOK == /\ now \in 0..MaxTime /\ cancelled \in BOOLEAN /\ pending \in BOOLEAN
          /\ issued \in 0..1 /\ attempts \in 0..1 /\ sent \in 0..1
          /\ finalizations \in 0..1 /\ publication \in Modes \cup {"NONE"}
DispatchAuthorized == sent = 1 => allowed
TimeoutBounded == sent = 1 => timeout > 0 /\ now + timeout <= Deadline
RejectedNeverSent == preflightCode # "" => issued = 0 /\ sent = 0 /\ knownZero /\ modelSent = FALSE
UnsentAccounting == (phase = "Done" /\ preflightCode # "") => attempts = 0
StorageFailurePreserved == (phase = "Done" /\ publication \in Modes \ {"ACK"}) =>
                           errorCode = "COMMIT_UNKNOWN" /\ operationId = "receipt-operation"
PartialPublicationRetained == publication \in {"ROLLBACK", "PHYSICAL_ACK_LOST"} => pending
Spec == Init /\ [][Next]_vars
=============================================================================
