------------------------ MODULE CaptureSettlement ------------------------
EXTENDS Integers
CONSTANTS TrustProviderStatus, SettlementResults
VARIABLES phase, ledger, provider, captureStatus, captureSettlement,
          settlementReceipt, workflowChecked, workflowReady
vars == <<phase, ledger, provider, captureStatus, captureSettlement,
          settlementReceipt, workflowChecked, workflowReady>>

Init == /\ phase = "Sent" /\ ledger = "STARTED" /\ provider = "NONE"
        /\ captureStatus = "STARTED" /\ captureSettlement = "NONE"
        /\ settlementReceipt = "NONE" /\ workflowChecked = FALSE /\ workflowReady = FALSE

Observe == /\ phase = "Sent" /\ provider' = "COMPLETE" /\ phase' = "Finish"
           /\ UNCHANGED <<ledger, captureStatus, captureSettlement, settlementReceipt,
                          workflowChecked, workflowReady>>
Finish(result) == /\ phase = "Finish" /\ result \in SettlementResults
                  /\ ledger' = IF result \in {"ACK", "LOST_COMMITTED"} THEN "COMPLETE" ELSE "STARTED"
                  /\ settlementReceipt' = IF result = "ACK" THEN "COMMITTED" ELSE "UNCONFIRMED"
                  /\ phase' = "Persist"
                  /\ UNCHANGED <<provider, captureStatus, captureSettlement, workflowChecked, workflowReady>>
Persist == /\ phase = "Persist" /\ captureStatus' = provider
           /\ captureSettlement' = settlementReceipt /\ phase' = "Check"
           /\ UNCHANGED <<ledger, provider, settlementReceipt, workflowChecked, workflowReady>>
CrashBeforeCapture == /\ phase \in {"Sent", "Finish", "Persist"} /\ phase' = "Check"
                      /\ UNCHANGED <<ledger, provider, captureStatus, captureSettlement,
                                     settlementReceipt, workflowChecked, workflowReady>>
CheckWorkflow == /\ phase = "Check" /\ workflowChecked' = TRUE /\ phase' = "Done"
                 /\ workflowReady' = (captureStatus = "COMPLETE" /\
                       (TrustProviderStatus \/ captureSettlement = "COMMITTED"))
                 /\ UNCHANGED <<ledger, provider, captureStatus, captureSettlement, settlementReceipt>>
Next == Observe \/ (\E result \in SettlementResults: Finish(result)) \/ Persist
        \/ CrashBeforeCapture \/ CheckWorkflow
TypeOK == /\ phase \in {"Sent", "Finish", "Persist", "Check", "Done"}
          /\ ledger \in {"STARTED", "COMPLETE"} /\ provider \in {"NONE", "COMPLETE"}
          /\ captureStatus \in {"STARTED", "COMPLETE"}
          /\ captureSettlement \in {"NONE", "COMMITTED", "UNCONFIRMED"}
          /\ settlementReceipt \in {"NONE", "COMMITTED", "UNCONFIRMED"}
          /\ workflowChecked \in BOOLEAN /\ workflowReady \in BOOLEAN
KnownProviderFactsRetained == captureStatus = "COMPLETE" => provider = "COMPLETE"
NoWorkflowReleaseWithoutConfirmedSettlement ==
    workflowChecked /\ captureSettlement = "UNCONFIRMED" => ~workflowReady
NoWorkflowReleaseBeforeResponse == workflowReady => provider = "COMPLETE" /\ ledger = "COMPLETE"
Spec == Init /\ [][Next]_vars
=============================================================================
