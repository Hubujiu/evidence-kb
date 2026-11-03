------------------------- MODULE SDKGatewayRequest -------------------------
EXTENDS Integers, FiniteSets, TLC

CONSTANTS Contexts, Payloads, AttemptCount, OperationCount,
          StaleSuccessStatus, WeakBinding, MutableContext, EarlyClose, IgnoreGate
Attempts == 1..AttemptCount
Keys == Contexts \X Payloads
NoKey == <<"none", "none">>
RecordStates == {"NONE", "STARTED", "COMPLETE", "ERROR", "UNKNOWN"}

VARIABLES lifecycle, configured, phase, operations, physical, current, captured,
          payload, seen, blocked, cache, rowKey, rowState, acknowledged,
          upstream, localError, wireStatus, errorBody, replayed, bindingRejected,
          admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext
vars == <<lifecycle, configured, phase, operations, physical, current, captured,
          payload, seen, blocked, cache, rowKey, rowState, acknowledged,
          upstream, localError, wireStatus, errorBody, replayed, bindingRejected,
          admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

RequestKey == <<captured, payload>>
Unresolved == \E a \in Attempts:
    rowState[a] \in {"STARTED", "UNKNOWN"} \/ (rowState[a] # "NONE" /\ ~acknowledged[a])

Init == /\ lifecycle = "OPEN" /\ configured \in Contexts /\ phase = "IDLE"
        /\ operations = 0 /\ physical = 0 /\ current = 0
        /\ captured = "none" /\ payload = "none" /\ seen = {} /\ blocked = FALSE
        /\ cache = [k \in Keys |-> 0]
        /\ rowKey = [a \in Attempts |-> NoKey]
        /\ rowState = [a \in Attempts |-> "NONE"]
        /\ acknowledged = [a \in Attempts |-> FALSE]
        /\ upstream = "none" /\ localError = FALSE
        /\ wireStatus = 0 /\ errorBody = FALSE /\ replayed = FALSE
        /\ bindingRejected = FALSE /\ admittedWhileBlocked = FALSE /\ captureActive = FALSE
        /\ closeRequested = FALSE /\ closeObservedActive = FALSE /\ reportedContext = "none"

SetContext(c) == /\ c \in Contexts /\ lifecycle = "OPEN"
                 /\ configured' = c
                 /\ UNCHANGED <<lifecycle, phase, operations, physical, current, captured,
                     payload, seen, blocked, cache, rowKey, rowState, acknowledged,
                     upstream, localError, wireStatus, errorBody, replayed, bindingRejected,
                     admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

Admit(p) == /\ p \in Payloads /\ phase = "IDLE" /\ lifecycle = "OPEN"
            /\ (~blocked \/ IgnoreGate) /\ operations < OperationCount
            /\ (<<configured, p>> \notin seen \/ cache[<<configured, p>>] # 0)
            /\ operations' = operations + 1 /\ captured' = configured /\ payload' = p
            /\ seen' = seen \cup {<<configured, p>>}
            /\ current' = cache[<<configured, p>>]
            /\ phase' = IF cache[<<configured, p>>] = 0 THEN "ADMITTED" ELSE "READY"
            /\ upstream' = IF cache[<<configured, p>>] = 0 THEN "none" ELSE "COMPLETE"
            /\ replayed' = (replayed \/ cache[<<configured, p>>] # 0)
            /\ localError' = FALSE /\ wireStatus' = 0 /\ errorBody' = FALSE
            /\ admittedWhileBlocked' = (admittedWhileBlocked \/ blocked)
            /\ UNCHANGED <<lifecycle, configured, physical, blocked, cache, rowKey, rowState,
                           acknowledged, bindingRejected, captureActive, closeRequested, closeObservedActive, reportedContext>>

RejectDuplicate(p) == /\ p \in Payloads /\ phase = "IDLE" /\ lifecycle = "OPEN"
                      /\ <<configured, p>> \in seen /\ cache[<<configured, p>>] = 0
                      /\ blocked' = TRUE
                      /\ UNCHANGED <<lifecycle, configured, phase, operations, physical, current,
                          captured, payload, seen, cache, rowKey, rowState, acknowledged,
                          upstream, localError, wireStatus, errorBody, replayed, bindingRejected,
                          admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

RejectMalformed == /\ lifecycle = "OPEN" /\ blocked' = TRUE
                   /\ UNCHANGED <<lifecycle, configured, phase, operations, physical, current,
                       captured, payload, seen, cache, rowKey, rowState, acknowledged,
                       upstream, localError, wireStatus, errorBody, replayed, bindingRejected,
                       admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

PreflightFailure == /\ phase = "ADMITTED" /\ phase' = "READY" /\ localError' = TRUE
                    /\ UNCHANGED <<lifecycle, configured, operations, physical, current,
                        captured, payload, seen, blocked, cache, rowKey, rowState, acknowledged,
                        upstream, wireStatus, errorBody, replayed, bindingRejected,
                        admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

Send == /\ phase = "ADMITTED" /\ physical < AttemptCount /\ ~Unresolved
        /\ physical' = physical + 1 /\ current' = physical + 1 /\ phase' = "SENT"
        /\ rowKey' = [rowKey EXCEPT ![physical + 1] =
            <<captured, payload>>]
        /\ rowState' = [rowState EXCEPT ![physical + 1] = "STARTED"]
        /\ captureActive' = TRUE
        /\ UNCHANGED <<lifecycle, configured, operations, captured, payload, seen, blocked,
            cache, acknowledged, upstream, localError, wireStatus, errorBody, replayed,
            bindingRejected, admittedWhileBlocked, closeRequested, closeObservedActive, reportedContext>>

Observe(result) == /\ phase = "SENT" /\ result \in {"COMPLETE", "ERROR", "UNKNOWN"}
                   /\ upstream' = result /\ phase' = "OBSERVED"
                   /\ UNCHANGED <<lifecycle, configured, operations, physical, current,
                       captured, payload, seen, blocked, cache, rowKey, rowState, acknowledged,
                       localError, wireStatus, errorBody, replayed, bindingRejected,
                       admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

Settle(ack) == /\ phase = "OBSERVED" /\ ack \in BOOLEAN
               /\ rowState' = [rowState EXCEPT ![current] = upstream]
               /\ acknowledged' = [acknowledged EXCEPT ![current] = ack]
               /\ localError' = (~ack \/ upstream = "UNKNOWN")
               /\ phase' = IF ack /\ upstream = "COMPLETE" THEN "BIND" ELSE "READY"
               /\ captureActive' = FALSE
               /\ UNCHANGED <<lifecycle, configured, operations, physical, current,
                   captured, payload, seen, blocked, cache, rowKey, upstream, wireStatus,
                   errorBody, replayed, bindingRejected, admittedWhileBlocked, closeRequested, closeObservedActive, reportedContext>>

Bind == /\ phase = "BIND"
        /\ LET matches == {a \in Attempts: rowState[a] = "COMPLETE" /\ rowKey[a][2] = payload}
               rejected == WeakBinding /\ Cardinality(matches) # 1
           IN /\ cache' = IF rejected THEN cache ELSE [cache EXCEPT ![RequestKey] = current]
              /\ localError' = rejected
              /\ bindingRejected' = (bindingRejected \/ rejected)
        /\ phase' = "READY"
        /\ UNCHANGED <<lifecycle, configured, operations, physical, current, captured,
            payload, seen, blocked, rowKey, rowState, acknowledged, upstream, wireStatus,
            errorBody, replayed, admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

CacheFailure == /\ phase = "BIND" /\ localError' = TRUE /\ phase' = "READY"
                /\ UNCHANGED <<lifecycle, configured, operations, physical, current,
                    captured, payload, seen, blocked, cache, rowKey, rowState, acknowledged,
                    upstream, wireStatus, errorBody, replayed, bindingRejected,
                    admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

Project(reportFailed) == /\ phase = "READY" /\ reportFailed \in BOOLEAN
                         /\ LET failed == localError \/ reportFailed
                            IN /\ localError' = failed
                               /\ errorBody' = (failed \/ upstream = "ERROR")
                               /\ wireStatus' = IF failed THEN
                                   IF StaleSuccessStatus /\ upstream = "COMPLETE" THEN 200 ELSE 500
                                   ELSE IF upstream = "COMPLETE" THEN 200 ELSE 502
                               /\ blocked' = (blocked \/ failed \/ upstream # "COMPLETE")
                         /\ phase' = "PROJECTED"
                         /\ reportedContext' = IF MutableContext THEN configured ELSE captured
                         /\ UNCHANGED <<lifecycle, configured, operations, physical, current,
                             captured, payload, seen, cache, rowKey, rowState, acknowledged,
                             upstream, replayed, bindingRejected, admittedWhileBlocked, captureActive, closeRequested, closeObservedActive>>

Finish == /\ phase = "PROJECTED" /\ phase' = "IDLE"
          /\ UNCHANGED <<lifecycle, configured, operations, physical, current, captured,
              payload, seen, blocked, cache, rowKey, rowState, acknowledged, upstream,
              localError, wireStatus, errorBody, replayed, bindingRejected,
              admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

RequestClose == /\ lifecycle = "OPEN" /\ ~closeRequested
                /\ closeRequested' = TRUE /\ closeObservedActive' = captureActive
                /\ UNCHANGED <<lifecycle, configured, phase, operations, physical, current,
                    captured, payload, seen, blocked, cache, rowKey, rowState, acknowledged,
                    upstream, localError, wireStatus, errorBody, replayed, bindingRejected,
                    admittedWhileBlocked, captureActive, reportedContext>>

BeginClose == /\ lifecycle = "OPEN" /\ closeRequested
              /\ (phase \in {"IDLE", "PROJECTED"} \/ (EarlyClose /\ captureActive))
              /\ lifecycle' = "DRAINING"
              /\ UNCHANGED <<configured, phase, operations, physical, current, captured,
                  payload, seen, blocked, cache, rowKey, rowState, acknowledged, upstream,
                  localError, wireStatus, errorBody, replayed, bindingRejected,
                  admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>
FinishClose == /\ lifecycle = "DRAINING" /\ (phase = "IDLE" \/ EarlyClose)
               /\ lifecycle' = "CLOSED"
               /\ UNCHANGED <<configured, phase, operations, physical, current, captured,
                   payload, seen, blocked, cache, rowKey, rowState, acknowledged, upstream,
                   localError, wireStatus, errorBody, replayed, bindingRejected,
                   admittedWhileBlocked, captureActive, closeRequested, closeObservedActive, reportedContext>>

Next == (\E c \in Contexts: SetContext(c)) \/ (\E p \in Payloads: Admit(p) \/ RejectDuplicate(p))
        \/ RejectMalformed \/ PreflightFailure \/ Send
        \/ (\E result \in {"COMPLETE", "ERROR", "UNKNOWN"}: Observe(result))
        \/ (\E ack \in BOOLEAN: Settle(ack)) \/ Bind \/ CacheFailure
        \/ (\E failed \in BOOLEAN: Project(failed)) \/ Finish \/ RequestClose \/ BeginClose \/ FinishClose

TypeOK == /\ lifecycle \in {"OPEN", "DRAINING", "CLOSED"}
          /\ configured \in Contexts /\ captured \in Contexts \cup {"none"}
          /\ payload \in Payloads \cup {"none"}
          /\ reportedContext \in Contexts \cup {"none"}
          /\ phase \in {"IDLE", "ADMITTED", "SENT", "OBSERVED", "BIND", "READY", "PROJECTED"}
          /\ operations \in 0..OperationCount /\ physical \in 0..AttemptCount /\ current \in 0..AttemptCount
          /\ seen \subseteq Keys /\ cache \in [Keys -> 0..AttemptCount]
          /\ rowKey \in [Attempts -> Keys \cup {NoKey}] /\ rowState \in [Attempts -> RecordStates]
          /\ acknowledged \in [Attempts -> BOOLEAN]
          /\ upstream \in {"none", "COMPLETE", "ERROR", "UNKNOWN"}
          /\ wireStatus \in {0, 200, 500, 502}
          /\ \A value \in {blocked, localError, errorBody, replayed, bindingRejected,
                           admittedWhileBlocked, captureActive, closeRequested, closeObservedActive}: value \in BOOLEAN
ErrorBodyIsFailure == errorBody => wireStatus >= 400
ExactCacheBinding == \A k \in Keys: cache[k] # 0 =>
    rowKey[cache[k]] = k /\ rowState[cache[k]] = "COMPLETE" /\ acknowledged[cache[k]]
CapturedContextStable == phase \in {"SENT", "OBSERVED", "BIND"} => rowKey[current] = RequestKey
ReportedContextStable == phase = "PROJECTED" => reportedContext = captured
NoSpuriousCrossContextRejection == ~bindingRejected
ClosedIsQuiescent == lifecycle = "CLOSED" => phase = "IDLE" /\ ~captureActive
NoAdmissionAfterGate == ~admittedWhileBlocked
NoDuplicatePhysicalSend == \A a, b \in Attempts:
    rowState[a] # "NONE" /\ rowState[b] # "NONE" /\ a # b => rowKey[a] # rowKey[b]
NeverReplayed == ~replayed
NeverTwoContextsCaptured == ~\E a, b \in Attempts:
    rowState[a] = "COMPLETE" /\ rowState[b] = "COMPLETE" /\ rowKey[a][1] # rowKey[b][1]
NeverDrainedCapture == ~(lifecycle = "CLOSED" /\ closeObservedActive /\ ~captureActive)
Spec == Init /\ [][Next]_vars
=============================================================================
