----------------------- MODULE AcceptanceExecution -----------------------
EXTENDS Naturals, Integers, FiniteSets, TLC
CONSTANTS IgnorePending, DropPriorCalls, PublishEarly, DropDenominator
VARIABLES pc, runStatus, caseNo, turnNo, requests, markers, physical, usage,
          response, capture, outcomes, traceCalls, traces, reports, manifest,
          denominator, failures, unsafeSend, reopened
vars == <<pc, runStatus, caseNo, turnNo, requests, markers, physical, usage,
          response, capture, outcomes, traceCalls, traces, reports, manifest,
          denominator, failures, unsafeSend, reopened>>
Cases == 1..2
Turns == 1..2
Keys == Cases \X Turns
Current == <<caseNo, turnNo>>
Unresolved == \E k \in Keys : physical[k] \in {"STARTED", "UNKNOWN"}
Calls(c) == Cardinality({t \in Turns : physical[<<c,t>>] # "ABSENT"})
Init == /\ pc = "admit" /\ runStatus = "RUNNING" /\ caseNo = 1 /\ turnNo = 1
        /\ requests = {} /\ markers = {} /\ response = {}
        /\ physical = [k \in Keys |-> "ABSENT"] /\ usage = [k \in Keys |-> -1]
        /\ capture = {} /\ outcomes = [c \in Cases |-> "UNRUN"]
        /\ traceCalls = [c \in Cases |-> 0] /\ traces = {} /\ reports = {}
        /\ manifest = FALSE /\ denominator = 0 /\ failures = 0 /\ unsafeSend = FALSE /\ reopened = FALSE
Admit == /\ pc = "admit"
         /\ pc' = IF Unresolved /\ ~IgnorePending THEN "trace" ELSE "session"
         /\ outcomes' = IF Unresolved /\ ~IgnorePending THEN [outcomes EXCEPT ![caseNo] = "NOT_RUN"] ELSE outcomes
         /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                        capture, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Session == /\ pc = "session" /\ pc' = "request"
           /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                          capture, outcomes, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Request == /\ pc = "request" /\ requests' = requests \cup {Current} /\ pc' = "marker"
           /\ UNCHANGED <<runStatus, caseNo, turnNo, markers, physical, usage, response,
                          capture, outcomes, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Marker == /\ pc = "marker" /\ markers' = markers \cup {Current} /\ pc' = "send"
          /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, physical, usage, response,
                         capture, outcomes, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Send == /\ pc = "send" /\ (~Unresolved \/ IgnorePending)
        /\ physical' = [physical EXCEPT ![Current] = "STARTED"]
        /\ unsafeSend' = (unsafeSend \/ Unresolved) /\ pc' = "observe"
        /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, usage, response,
                       capture, outcomes, traceCalls, traces, reports, manifest, denominator, failures, reopened>>
Observe(unknown) == /\ pc = "observe" /\ unknown \in BOOLEAN /\ (~unknown \/ failures = 0)
                    /\ physical' = [physical EXCEPT ![Current] = IF unknown THEN "UNKNOWN" ELSE "COMPLETE"]
                    /\ usage' = [usage EXCEPT ![Current] = IF unknown THEN -1 ELSE 1]
                    /\ failures' = failures + IF unknown THEN 1 ELSE 0
                    /\ pc' = "response"
                    /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, response,
                                   capture, outcomes, traceCalls, traces, reports, manifest, denominator, unsafeSend, reopened>>
Response == /\ pc = "response" /\ response' = response \cup {Current}
            /\ pc' = IF physical[Current] = "UNKNOWN" THEN "trace"
                      ELSE IF turnNo = 2 THEN "capture" ELSE "request"
            /\ outcomes' = IF physical[Current] = "UNKNOWN" THEN [outcomes EXCEPT ![caseNo] = "ERROR"] ELSE outcomes
            /\ turnNo' = IF physical[Current] # "UNKNOWN" /\ turnNo = 1 THEN 2 ELSE turnNo
            /\ UNCHANGED <<runStatus, caseNo, requests, markers, physical, usage,
                           capture, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Capture == /\ pc = "capture" /\ capture' = capture \cup {caseNo}
           /\ outcomes' = [outcomes EXCEPT ![caseNo] = "OK"] /\ pc' = "trace"
           /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                          traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Failure == /\ pc \in {"session", "request", "marker", "send", "response", "capture"}
           /\ failures = 0 /\ failures' = 1 /\ pc' = "trace"
           /\ outcomes' = [outcomes EXCEPT ![caseNo] = "ERROR"]
           /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                          capture, traceCalls, traces, reports, manifest, denominator, unsafeSend, reopened>>
Trace == /\ pc = "trace" /\ traces' = traces \cup {caseNo} /\ pc' = "next"
         /\ traceCalls' = [traceCalls EXCEPT ![caseNo] =
              IF DropPriorCalls /\ outcomes[caseNo] = "ERROR" /\ Calls(caseNo) = 2 THEN 1 ELSE Calls(caseNo)]
         /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                        capture, outcomes, reports, manifest, denominator, failures, unsafeSend, reopened>>
NextCase == /\ pc = "next" /\ pc' = IF caseNo = 2 THEN "reports" ELSE "admit"
            /\ caseNo' = 2 /\ turnNo' = 1
            /\ UNCHANGED <<runStatus, requests, markers, physical, usage, response,
                           capture, outcomes, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Report == /\ pc = "reports" /\ reports' = reports \cup {Cardinality(reports) + 1}
          /\ pc' = IF Cardinality(reports) = 2 THEN "manifest" ELSE "reports"
          /\ denominator' = IF DropDenominator THEN Cardinality({c \in Cases : outcomes[c] # "NOT_RUN"}) ELSE 2
          /\ runStatus' = IF PublishEarly THEN "COMPLETE" ELSE runStatus
          /\ UNCHANGED <<caseNo, turnNo, requests, markers, physical, usage, response,
                         capture, outcomes, traceCalls, traces, manifest, failures, unsafeSend, reopened>>
Manifest == /\ pc = "manifest" /\ manifest' = TRUE /\ pc' = "complete"
            /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                           capture, outcomes, traceCalls, traces, reports, denominator, failures, unsafeSend, reopened>>
Complete == /\ pc = "complete" /\ runStatus' = "COMPLETE" /\ pc' = "done"
            /\ UNCHANGED <<caseNo, turnNo, requests, markers, physical, usage, response,
                           capture, outcomes, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend, reopened>>
Crash == /\ pc \notin {"done", "crashed"} /\ failures = 0
         /\ pc' = "crashed" /\ failures' = 1
         /\ UNCHANGED <<runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                        capture, outcomes, traceCalls, traces, reports, manifest, denominator, unsafeSend, reopened>>
RejectExistingOutput == /\ pc \in {"done", "crashed"} /\ ~reopened /\ reopened' = TRUE
                        /\ UNCHANGED <<pc, runStatus, caseNo, turnNo, requests, markers, physical, usage, response,
                                       capture, outcomes, traceCalls, traces, reports, manifest, denominator, failures, unsafeSend>>
Next == Admit \/ Session \/ Request \/ Marker \/ Send \/ (\E unknown \in BOOLEAN : Observe(unknown)) \/
        Response \/ Capture \/ Failure \/ Trace \/ NextCase \/ Report \/ Manifest \/ Complete \/ Crash \/ RejectExistingOutput
Spec == Init /\ [][Next]_vars
TypeOK == /\ pc \in {"admit", "session", "request", "marker", "send", "observe", "response", "capture", "trace", "next", "reports", "manifest", "complete", "done", "crashed"}
          /\ manifest \in BOOLEAN /\ unsafeSend \in BOOLEAN /\ reopened \in BOOLEAN
          /\ runStatus \in {"RUNNING", "COMPLETE"} /\ caseNo \in Cases /\ turnNo \in Turns
          /\ physical \in [Keys -> {"ABSENT", "STARTED", "COMPLETE", "UNKNOWN"}]
          /\ usage \in [Keys -> {-1, 1}] /\ requests \subseteq Keys /\ markers \subseteq Keys /\ response \subseteq Keys
          /\ capture \subseteq Cases /\ traces \subseteq Cases /\ reports \subseteq 1..3
          /\ outcomes \in [Cases -> {"UNRUN", "OK", "ERROR", "NOT_RUN"}]
          /\ traceCalls \in [Cases -> 0..2] /\ failures \in 0..1 /\ denominator \in 0..2
MarkerBeforeSend == \A k \in Keys : physical[k] # "ABSENT" => k \in requests /\ k \in markers
PendingBlocksSend == ~unsafeSend
TraceRetainsAllCalls == \A c \in traces : traceCalls[c] = Calls(c)
CaptureRequiresAllTurns == \A c \in capture : \A t \in Turns : physical[<<c,t>>] = "COMPLETE" /\ <<c,t>> \in response
CompletePublication == runStatus = "COMPLETE" => traces = Cases /\ reports = 1..3 /\ manifest
FixedDenominator == reports # {} => denominator = 2
NoCompleteWitness == ~(runStatus = "COMPLETE" /\ \A c \in Cases : outcomes[c] = "OK" /\ traceCalls[c] = 2)
NoBlockedWitness == ~(runStatus = "COMPLETE" /\ outcomes[1] = "ERROR" /\ outcomes[2] = "NOT_RUN" /\ traceCalls[1] = 2 /\ usage[<<1,1>>] = 1 /\ usage[<<1,2>>] = -1)
=============================================================================
