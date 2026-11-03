-------------------------- MODULE SQLiteBootstrap --------------------------
EXTENDS Naturals
CONSTANTS EarlyPublish, RetryOther, ResetBudget
Budget == 2
VARIABLES pc, managed, mode, confirmed, settings, remaining, spent, attempts,
          lastError, badRetry
vars == <<pc, managed, mode, confirmed, settings, remaining, spent, attempts,
          lastError, badRetry>>
Target == IF managed THEN "wal" ELSE "delete"
Init == /\ pc = "probe" /\ managed \in BOOLEAN /\ mode \in {"wal", "delete"}
        /\ confirmed = FALSE /\ settings = FALSE
        /\ remaining = Budget /\ spent = 0 /\ attempts = 1
        /\ lastError = "none" /\ badRetry = FALSE
ReadMatch == /\ pc = "probe" /\ mode = Target
             /\ pc' = "configured" /\ confirmed' = TRUE
             /\ UNCHANGED <<managed, mode, settings, remaining, spent, attempts, lastError, badRetry>>
ReadMismatch == /\ pc = "probe" /\ mode # Target /\ pc' = "changing"
                /\ UNCHANGED <<managed, mode, confirmed, settings, remaining, spent, attempts, lastError, badRetry>>
ChangeMode == /\ pc = "changing" /\ mode' = Target
              /\ pc' = "configured" /\ confirmed' = TRUE
              /\ UNCHANGED <<managed, settings, remaining, spent, attempts, lastError, badRetry>>
Busy == /\ pc \in {"probe", "changing"}
        /\ lastError' = "busy" /\ pc' = "waiting"
        /\ UNCHANGED <<managed, mode, confirmed, settings, remaining, spent, attempts, badRetry>>
OtherError == /\ pc \in {"probe", "changing"}
              /\ lastError' = "other"
              /\ pc' = IF RetryOther THEN "waiting" ELSE "error"
              /\ UNCHANGED <<managed, mode, confirmed, settings, remaining, spent, attempts, badRetry>>
Retry == /\ pc = "waiting" /\ remaining > 0 /\ attempts < 3
         /\ pc' = "probe" /\ attempts' = attempts + 1
         /\ remaining' = IF ResetBudget THEN Budget ELSE remaining - 1
         /\ spent' = spent + 1
         /\ badRetry' = (badRetry \/ lastError # "busy")
         /\ UNCHANGED <<managed, mode, confirmed, settings, lastError>>
Timeout == /\ pc = "waiting" /\ remaining = 0 /\ pc' = "error"
           /\ UNCHANGED <<managed, mode, confirmed, settings, remaining, spent, attempts, lastError, badRetry>>
EnvironmentCompletesMode == /\ pc = "waiting" /\ mode # Target /\ mode' = Target
                           /\ UNCHANGED <<pc, managed, confirmed, settings, remaining, spent, attempts, lastError, badRetry>>
Publish == /\ pc = "configured" /\ pc' = "ready" /\ settings' = TRUE
           /\ UNCHANGED <<managed, mode, confirmed, remaining, spent, attempts, lastError, badRetry>>
PrematurePublish == /\ EarlyPublish /\ pc \in {"probe", "changing"} /\ pc' = "ready"
                    /\ UNCHANGED <<managed, mode, confirmed, settings, remaining, spent, attempts, lastError, badRetry>>
Next == ReadMatch \/ ReadMismatch \/ ChangeMode \/ Busy \/ OtherError \/ Retry \/ Timeout
        \/ EnvironmentCompletesMode \/ Publish \/ PrematurePublish
Spec == Init /\ [][Next]_vars
TypeOK == /\ pc \in {"probe", "changing", "configured", "waiting", "ready", "error"}
          /\ managed \in BOOLEAN /\ mode \in {"wal", "delete"}
          /\ confirmed \in BOOLEAN /\ settings \in BOOLEAN /\ badRetry \in BOOLEAN
          /\ remaining \in 0..Budget /\ spent \in 0..Budget /\ attempts \in 1..3
          /\ lastError \in {"none", "busy", "other"}
PublishedConfigured == pc = "ready" => confirmed /\ settings /\ mode = Target
OnlyBusyRetry == ~badRetry
FixedBudget == remaining + spent = Budget
NoRetrySuccessWitness == ~(pc = "ready" /\ attempts > 1)
NoExhaustedWitness == ~(pc = "error" /\ lastError = "busy" /\ remaining = 0)
=============================================================================
