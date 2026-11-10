---------------------- MODULE ObservedSettlementFacts ----------------------
EXTENDS Naturals

CONSTANTS DropObservedUsage, MarkPostSendUnsent

UsageKinds == {"missing", "unknown", "partial", "known"}
CommitResults == {"ack", "rollback", "lost_commit", "lost_rollback"}
CommitFailures == CommitResults \ {"ack"}

VARIABLES phase, reserveResult, budgetState, sends, observed, observedUsage, settlementResult, captureResult, errorOrigin, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown
vars == <<phase, reserveResult, budgetState, sends, observed, observedUsage, settlementResult, captureResult, errorOrigin, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

Init ==
    /\ phase = "Reserve"
    /\ reserveResult = "none"
    /\ budgetState = "Absent"
    /\ sends = 0
    /\ observed = FALSE
    /\ observedUsage = "missing"
    /\ settlementResult = "none"
    /\ captureResult = "none"
    /\ errorOrigin = "none"
    /\ reportedSent = "unpublished"
    /\ reportedUsage = "missing"
    /\ upperCalls = 0
    /\ upperTokens = 0
    /\ upperUnknown = 0

Reserve(result) ==
    /\ phase = "Reserve" /\ result \in CommitResults
    /\ phase' = IF result = "ack" THEN "PreCapture" ELSE "Publish"
    /\ reserveResult' = result
    /\ budgetState' = IF result \in {"ack", "lost_commit"} THEN "Started" ELSE "Absent"
    /\ errorOrigin' = IF result = "ack" THEN "none" ELSE "reserve"
    /\ UNCHANGED <<sends, observed, observedUsage, settlementResult, captureResult, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

PreCapture(ok) ==
    /\ phase = "PreCapture"
    /\ phase' = IF ok THEN "Dispatch" ELSE "Publish"
    /\ captureResult' = IF ok THEN "none" ELSE "failed"
    /\ errorOrigin' = IF ok THEN "none" ELSE "pre_capture"
    /\ UNCHANGED <<reserveResult, budgetState, sends, observed, observedUsage, settlementResult, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

Dispatch ==
    /\ phase = "Dispatch" /\ reserveResult = "ack" /\ budgetState = "Started"
    /\ phase' = "Response"
    /\ sends' = 1
    /\ UNCHANGED <<reserveResult, budgetState, observed, observedUsage, settlementResult, captureResult, errorOrigin, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

Receive(usage) ==
    /\ phase = "Response" /\ usage \in UsageKinds
    /\ phase' = "Settle"
    /\ observed' = TRUE
    /\ observedUsage' = usage
    /\ UNCHANGED <<reserveResult, budgetState, sends, settlementResult, captureResult, errorOrigin, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

LoseResponse ==
    /\ phase = "Response"
    /\ phase' = "Settle"
    /\ errorOrigin' = "transport"
    /\ UNCHANGED <<reserveResult, budgetState, sends, observed, observedUsage, settlementResult, captureResult, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

Settle(result) ==
    /\ phase = "Settle" /\ result \in CommitResults
    /\ phase' = "Capture"
    /\ settlementResult' = result
    /\ budgetState' = IF result \in {"ack", "lost_commit"}
                       THEN (IF observed THEN "Terminal" ELSE "Unknown") ELSE "Started"
    /\ errorOrigin' = IF result = "ack" THEN errorOrigin ELSE "settlement"
    /\ UNCHANGED <<reserveResult, sends, observed, observedUsage, captureResult, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

Capture(ok) ==
    /\ phase = "Capture"
    /\ phase' = "Publish"
    /\ captureResult' = IF ok THEN "published" ELSE "failed"
    /\ errorOrigin' = IF ~ok /\ errorOrigin = "none" THEN "capture" ELSE errorOrigin
    /\ UNCHANGED <<reserveResult, budgetState, sends, observed, observedUsage, settlementResult, reportedSent, reportedUsage, upperCalls, upperTokens, upperUnknown>>

Publish ==
    LET sent == IF sends = 0 THEN "unsent"
                ELSE IF MarkPostSendUnsent /\ errorOrigin # "none" THEN "unsent"
                ELSE IF observed THEN "sent" ELSE "unknown"
        usage == IF observed
                 THEN IF DropObservedUsage /\ errorOrigin # "none" THEN "missing" ELSE observedUsage
                 ELSE "missing"
        calls == IF sent = "unsent" THEN 0 ELSE sends
        known == calls = 1 /\ usage = "known"
    IN /\ phase = "Publish"
       /\ phase' = "Done"
       /\ reportedSent' = sent
       /\ reportedUsage' = usage
       /\ upperCalls' = calls
       /\ upperTokens' = IF known THEN 2 ELSE 0
       /\ upperUnknown' = calls - (IF known THEN 1 ELSE 0)
       /\ UNCHANGED <<reserveResult, budgetState, sends, observed, observedUsage, settlementResult, captureResult, errorOrigin>>

Next ==
    \/ (\E result \in CommitResults : Reserve(result))
    \/ (\E ok \in BOOLEAN : PreCapture(ok))
    \/ Dispatch
    \/ (\E usage \in UsageKinds : Receive(usage))
    \/ LoseResponse
    \/ (\E result \in CommitResults : Settle(result))
    \/ (\E ok \in BOOLEAN : Capture(ok))
    \/ Publish
    \/ (phase = "Done" /\ UNCHANGED vars)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in {"Reserve", "PreCapture", "Dispatch", "Response", "Settle", "Capture", "Publish", "Done"}
    /\ reserveResult \in CommitResults \cup {"none"}
    /\ budgetState \in {"Absent", "Started", "Terminal", "Unknown"}
    /\ sends \in 0..1
    /\ observed \in BOOLEAN
    /\ observedUsage \in UsageKinds
    /\ settlementResult \in CommitResults \cup {"none"}
    /\ captureResult \in {"none", "published", "failed"}
    /\ errorOrigin \in {"none", "reserve", "pre_capture", "transport", "settlement", "capture"}
    /\ reportedSent \in {"unpublished", "unsent", "sent", "unknown"}
    /\ reportedUsage \in UsageKinds
    /\ upperCalls \in 0..1
    /\ upperTokens \in 0..2
    /\ upperUnknown \in 0..1

SendRequiresReserveAck == sends = 1 => reserveResult = "ack"

ReserveUnknownRetainsAuthority ==
    /\ (reserveResult = "lost_commit" => budgetState = "Started" /\ sends = 0)
    /\ (reserveResult = "lost_rollback" => budgetState = "Absent" /\ sends = 0)

KnownZeroSendAccounting ==
    phase = "Done" /\ sends = 0 =>
        reportedSent = "unsent" /\ upperCalls = 0 /\ upperTokens = 0 /\ upperUnknown = 0

PostSendNeverReportedUnsent == phase = "Done" /\ sends = 1 => reportedSent # "unsent"

ObservedResponseReported == phase = "Done" /\ observed => reportedSent = "sent"

ObservedUsagePreserved ==
    phase = "Done" /\ observed /\ observedUsage \in {"partial", "known"} => reportedUsage = observedUsage

NoInventedUsage == phase = "Done" => reportedUsage = (IF observed THEN observedUsage ELSE "missing")

AccountingConservative ==
    phase = "Done" =>
        /\ upperCalls = sends
        /\ upperTokens = (IF observed /\ observedUsage = "known" THEN 2 ELSE 0)
        /\ upperUnknown = (IF sends = 1 /\ ~(observed /\ observedUsage = "known") THEN 1 ELSE 0)

PriorFailureNotMasked ==
    /\ (phase \in {"Capture", "Publish", "Done"} /\ settlementResult \in CommitFailures =>
            errorOrigin = "settlement")
    /\ (phase \in {"Capture", "Publish", "Done"} /\ sends = 1 /\ ~observed /\ settlementResult = "ack" =>
            errorOrigin = "transport")
=============================================================================
