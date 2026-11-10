-------------------------- MODULE GoogleRetryAuth --------------------------
EXTENDS Naturals, Sequences, TLC
CONSTANTS ResetRetryBudget, RepeatAuthentication, CountOAuthAsModel
VARIABLES pc, calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled
vars == <<pc, calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
AuthCap == IF RepeatAuthentication THEN 2 ELSE 1
Init ==
    /\ pc = "ready" /\ calls = 0 /\ records = <<>> /\ retries = 0
    /\ authStages = 0 /\ authFlag = FALSE /\ authInvocations = 0
    /\ oauthCallbacks = 0 /\ oauthThis = 0 /\ lastKind = "none"
    /\ retryReason = "initial" /\ cancelled = FALSE
Dispatch ==
    /\ pc = "ready" /\ ~cancelled /\ calls < 5
    /\ pc' = "running"
    /\ calls' = calls + 1
    /\ records' = Append(records, [id |-> calls + 1, parent |-> calls, authStages |-> authStages, authRetry |-> authFlag, reason |-> retryReason, outcome |-> "STARTED"])
    /\ authFlag' = FALSE
    /\ UNCHANGED <<retries, authStages, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
Observe(kind) ==
    /\ pc = "running"
    /\ pc' = "assess"
    /\ lastKind' = kind
    /\ records' = [records EXCEPT ![Len(records)].outcome = IF kind = "unknown" THEN "UNKNOWN" ELSE IF kind = "success" THEN "COMPLETE" ELSE "ERROR"]
    /\ UNCHANGED <<calls, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, retryReason, cancelled>>
NeedAuthentication ==
    /\ pc = "assess" /\ lastKind = "401" /\ authStages < AuthCap
    /\ pc' = "headers"
    /\ retryReason' = "authentication"
    /\ UNCHANGED <<calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, cancelled>>
StartRefresh ==
    /\ pc = "headers"
    /\ pc' = "oauth"
    /\ authInvocations' = authInvocations + 1
    /\ oauthThis' = 0
    /\ UNCHANGED <<calls, records, retries, authStages, authFlag, oauthCallbacks, lastKind, retryReason, cancelled>>
OAuthAttempt ==
    /\ pc = "oauth" /\ oauthThis < 3
    /\ oauthThis' = oauthThis + 1
    /\ oauthCallbacks' = oauthCallbacks + 1
    /\ calls' = IF CountOAuthAsModel THEN calls + 1 ELSE calls
    /\ UNCHANGED <<pc, records, retries, authStages, authFlag, authInvocations, lastKind, retryReason, cancelled>>
RefreshComplete ==
    /\ pc = "oauth" /\ oauthThis > 0
    /\ pc' = "ready"
    /\ authStages' = authStages + 1
    /\ authFlag' = TRUE
    /\ retries' = IF ResetRetryBudget THEN 0 ELSE retries
    /\ UNCHANGED <<calls, records, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
AdoptCurrentToken ==
    /\ pc = "headers"
    /\ pc' = "ready"
    /\ authStages' = authStages + 1
    /\ authFlag' = TRUE
    /\ retries' = IF ResetRetryBudget THEN 0 ELSE retries
    /\ UNCHANGED <<calls, records, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
RefreshFailed ==
    /\ pc = "oauth" /\ oauthThis > 0
    /\ pc' = "terminal"
    /\ UNCHANGED <<calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
TransportRetry ==
    /\ pc = "assess" /\ lastKind \in {"503", "not_sent"} /\ retries < 1
    /\ pc' = "waiting"
    /\ retries' = retries + 1
    /\ retryReason' = lastKind
    /\ UNCHANGED <<calls, records, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, cancelled>>
WaitComplete ==
    /\ pc = "waiting"
    /\ pc' = "ready"
    /\ UNCHANGED <<calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
WaitRefused ==
    /\ pc = "waiting"
    /\ pc' = "terminal"
    /\ UNCHANGED <<calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
Finish ==
    /\ pc = "assess" /\ (lastKind \in {"success", "permanent", "unknown"} \/ (lastKind = "401" /\ authStages = AuthCap) \/ (lastKind \in {"503", "not_sent"} /\ retries = 1))
    /\ pc' = "terminal"
    /\ UNCHANGED <<calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
Cancel ==
    /\ pc # "terminal" /\ ~cancelled
    /\ cancelled' = TRUE
    /\ UNCHANGED <<pc, calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason>>
Refuse ==
    /\ pc = "ready" /\ cancelled
    /\ pc' = "terminal"
    /\ UNCHANGED <<calls, records, retries, authStages, authFlag, authInvocations, oauthCallbacks, oauthThis, lastKind, retryReason, cancelled>>
Next == Dispatch \/ NeedAuthentication \/ StartRefresh \/ OAuthAttempt \/ RefreshComplete
    \/ AdoptCurrentToken \/ RefreshFailed \/ TransportRetry \/ WaitComplete \/ WaitRefused \/ Finish \/ Cancel \/ Refuse
    \/ (\E kind \in {"401", "503", "not_sent", "unknown", "success", "permanent"}: Observe(kind))
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ pc \in {"ready", "running", "assess", "headers", "oauth", "waiting", "terminal"}
    /\ calls \in 0..11 /\ retries \in 0..1 /\ authStages \in 0..2
    /\ authFlag \in BOOLEAN /\ cancelled \in BOOLEAN
    /\ authInvocations \in 0..2 /\ oauthThis \in 0..3 /\ oauthCallbacks \in 0..6
ModelAccounting == calls = Len(records)
SharedRetryBudget == Len(records) <= 3
SingleAuthenticationStage == \A i \in 1..Len(records): records[i].authStages <= 1
NoUnknownReplay == \A i \in 1..(Len(records)-1): records[i].outcome \notin {"UNKNOWN", "STARTED"}
LinkedPhysicalAttempts == \A i \in 1..Len(records): records[i].id = i /\ records[i].parent = i - 1
OAuthInvocationBound == oauthCallbacks <= 3 * authInvocations
SuccessUnreachable == ~(pc = "terminal" /\ lastKind = "success" /\ calls = 3 /\ authStages = 1 /\ retries = 1)
=============================================================================
