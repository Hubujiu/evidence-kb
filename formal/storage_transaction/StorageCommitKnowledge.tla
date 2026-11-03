---------------------- MODULE StorageCommitKnowledge ----------------------
EXTENDS Naturals, TLC
CONSTANT LegacyRollbackFallback
VARIABLES phase, request, actual, acknowledged, cached, observed, receiptId,
          exitCalls, observations, replayAllowed
vars == <<phase, request, actual, acknowledged, cached, observed, receiptId,
          exitCalls, observations, replayAllowed>>
Init == /\ phase = "Open"
        /\ request = "None"
        /\ actual = "Active"
        /\ acknowledged = "None"
        /\ cached = "UNKNOWN"
        /\ observed = "None"
        /\ receiptId = "physical-operation"
        /\ exitCalls = 0
        /\ observations = 0
        /\ replayAllowed = FALSE
RequestExit(kind) ==
    /\ phase = "Open"
    /\ kind \in {"Commit", "Rollback"}
    /\ phase' = "Exiting"
    /\ request' = kind
    /\ exitCalls' = exitCalls + 1
    /\ UNCHANGED <<actual, acknowledged, cached, observed, receiptId,
                    observations, replayAllowed>>
BackendEffect(outcome) ==
    /\ phase = "Exiting"
    /\ outcome \in {"Active", "COMMITTED", "ROLLED_BACK"}
    /\ (request = "Rollback" => outcome \in {"Active", "ROLLED_BACK"})
    /\ actual' = outcome
    /\ phase' = "AwaitingExit"
    /\ UNCHANGED <<request, acknowledged, cached, observed, receiptId,
                    exitCalls, observations, replayAllowed>>
NormalExit ==
    /\ phase = "AwaitingExit"
    /\ actual = IF request = "Commit" THEN "COMMITTED" ELSE "ROLLED_BACK"
    /\ phase' = "Finished"
    /\ acknowledged' = actual
    /\ cached' = actual
    /\ UNCHANGED <<request, actual, observed, receiptId, exitCalls,
                    observations, replayAllowed>>
FailedExit(providerUnknown) ==
    /\ phase = "AwaitingExit"
    /\ providerUnknown \in BOOLEAN
    /\ phase' = "Finished"
    /\ cached' = IF LegacyRollbackFallback /\ ~providerUnknown
                  THEN "ROLLED_BACK" ELSE "UNKNOWN"
    /\ UNCHANGED <<request, actual, acknowledged, observed, receiptId,
                    exitCalls, observations, replayAllowed>>
Timeout ==
    /\ phase \in {"Exiting", "AwaitingExit"}
    /\ observed = "None"
    /\ observed' = "UNKNOWN"
    /\ UNCHANGED <<phase, request, actual, acknowledged, cached, receiptId,
                    exitCalls, observations, replayAllowed>>
Observe(validEvidence) ==
    /\ phase = "Finished"
    /\ observations < 2
    /\ validEvidence \in BOOLEAN
    /\ observed' = IF validEvidence THEN cached ELSE "UNKNOWN"
    /\ observations' = observations + 1
    /\ UNCHANGED <<phase, request, actual, acknowledged, cached, receiptId,
                    exitCalls, replayAllowed>>
Next == \/ \E kind \in {"Commit", "Rollback"}: RequestExit(kind)
        \/ \E outcome \in {"Active", "COMMITTED", "ROLLED_BACK"}: BackendEffect(outcome)
        \/ NormalExit
        \/ \E unknown \in BOOLEAN: FailedExit(unknown)
        \/ Timeout
        \/ \E valid \in BOOLEAN: Observe(valid)
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"Open", "Exiting", "AwaitingExit", "Finished"}
          /\ request \in {"None", "Commit", "Rollback"}
          /\ actual \in {"Active", "COMMITTED", "ROLLED_BACK"}
          /\ acknowledged \in {"None", "COMMITTED", "ROLLED_BACK"}
          /\ cached \in {"UNKNOWN", "COMMITTED", "ROLLED_BACK"}
          /\ observed \in {"None", "UNKNOWN", "COMMITTED", "ROLLED_BACK"}
          /\ exitCalls \in 0..1 /\ observations \in 0..2
          /\ replayAllowed \in BOOLEAN
NoUnsupportedReceipt == cached \in {"COMMITTED", "ROLLED_BACK"}
                        => acknowledged = cached /\ actual = cached
ObserverNeedsEvidence == observed \in {"COMMITTED", "ROLLED_BACK"}
                         => observed = cached /\ acknowledged = observed
NoReplay == exitCalls <= 1 /\ ~replayAllowed
IdentityPreserved == receiptId = "physical-operation"
=============================================================================
