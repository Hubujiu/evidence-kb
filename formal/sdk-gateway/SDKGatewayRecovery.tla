------------------------- MODULE SDKGatewayRecovery -------------------------
EXTENDS Integers, FiniteSets, TLC

CONSTANTS Contexts, Payloads, TrustTerminalStatus, RetryUnknown,
          PayloadOnlyRecovery, ReusePhysicalIdentity
Keys == Contexts \X Payloads
States == {"NONE", "STARTED", "COMPLETE", "ERROR", "UNKNOWN"}

VARIABLES phase, oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
          rejected, cacheSeeded, retrySeeded, requestContext, requestPayload,
          decision, retryOf, newIdentity, newSends, reusedIdentity
vars == <<phase, oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
          rejected, cacheSeeded, retrySeeded, requestContext, requestPayload,
          decision, retryOf, newIdentity, newSends, reusedIdentity>>

OldKey == <<oldContext, oldPayload>>
RequestKey == <<requestContext, requestPayload>>
Unresolved == oldState \notin {"COMPLETE", "ERROR"} \/ ~oldAck

Init == /\ phase = "OLD_READY" /\ oldContext \in Contexts /\ oldPayload \in Payloads
        /\ oldState = "NONE" /\ oldHttp = "NONE" /\ oldAck = FALSE /\ oldSends = 0
        /\ rejected = FALSE /\ cacheSeeded = FALSE /\ retrySeeded = FALSE
        /\ requestContext = "none" /\ requestPayload = "none" /\ decision = "none"
        /\ retryOf = 0 /\ newIdentity = 0 /\ newSends = 0 /\ reusedIdentity = 0

SendOld == /\ phase = "OLD_READY" /\ oldState' = "STARTED" /\ oldSends' = 1
           /\ phase' = "OLD_SENT"
           /\ UNCHANGED <<oldContext, oldPayload, oldHttp, oldAck, rejected, cacheSeeded,
               retrySeeded, requestContext, requestPayload, decision, retryOf, newIdentity,
               newSends, reusedIdentity>>

ObserveOld(result) == /\ phase = "OLD_SENT" /\ result \in {"OK", "OTHER_SUCCESS", "HTTP_ERROR", "UNKNOWN"}
                      /\ oldHttp' = result /\ phase' = "OLD_OBSERVED"
                      /\ UNCHANGED <<oldContext, oldPayload, oldState, oldAck, oldSends,
                          rejected, cacheSeeded, retrySeeded, requestContext, requestPayload,
                          decision, retryOf, newIdentity, newSends, reusedIdentity>>

SettleOld(ack) == /\ phase = "OLD_OBSERVED" /\ ack \in BOOLEAN
                 /\ oldState' = (CASE oldHttp \in {"OK", "OTHER_SUCCESS"} -> "COMPLETE"
                                     [] oldHttp = "HTTP_ERROR" -> "ERROR"
                                     [] OTHER -> "UNKNOWN")
                 /\ oldAck' = ack /\ phase' = "FROZEN"
                 /\ UNCHANGED <<oldContext, oldPayload, oldHttp, oldSends, rejected,
                     cacheSeeded, retrySeeded, requestContext, requestPayload, decision,
                     retryOf, newIdentity, newSends, reusedIdentity>>

CrashOld == /\ phase \in {"OLD_SENT", "OLD_OBSERVED"} /\ phase' = "FROZEN"
            /\ UNCHANGED <<oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
                rejected, cacheSeeded, retrySeeded, requestContext, requestPayload,
                decision, retryOf, newIdentity, newSends, reusedIdentity>>

LoadRecovery == /\ phase = "FROZEN"
                /\ LET allow == ((~Unresolved)
                         \/ (TrustTerminalStatus /\ oldState \in {"COMPLETE", "ERROR"})
                         \/ (RetryUnknown /\ oldState \in {"STARTED", "UNKNOWN"}))
                         /\ oldHttp # "OTHER_SUCCESS"
                   IN /\ rejected' = ~allow
                      /\ cacheSeeded' = (allow /\ oldState = "COMPLETE")
                      /\ retrySeeded' = (allow /\ oldState # "COMPLETE")
                /\ phase' = "LOADED"
                /\ UNCHANGED <<oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
                    requestContext, requestPayload, decision, retryOf, newIdentity,
                    newSends, reusedIdentity>>

Request(c, p) == /\ phase = "LOADED" /\ c \in Contexts /\ p \in Payloads
                 /\ requestContext' = c /\ requestPayload' = p /\ phase' = "REQUESTED"
                 /\ UNCHANGED <<oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
                     rejected, cacheSeeded, retrySeeded, decision, retryOf, newIdentity,
                     newSends, reusedIdentity>>

Decide == /\ phase = "REQUESTED"
          /\ LET exact == IF PayloadOnlyRecovery THEN requestPayload = oldPayload
                          ELSE RequestKey = OldKey
             IN /\ decision' = IF rejected THEN "REJECT"
                               ELSE IF cacheSeeded /\ exact THEN "REPLAY" ELSE "SEND"
                /\ retryOf' = IF ~rejected /\ retrySeeded /\ exact THEN 1 ELSE 0
                /\ reusedIdentity' = IF ~rejected /\ cacheSeeded /\ exact THEN 1 ELSE 0
          /\ phase' = "DECIDED"
          /\ UNCHANGED <<oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
              rejected, cacheSeeded, retrySeeded, requestContext, requestPayload,
              newIdentity, newSends>>

SendNew == /\ phase = "DECIDED" /\ decision = "SEND"
           /\ newIdentity' = IF ReusePhysicalIdentity /\ retryOf = 1 THEN 1 ELSE 2
           /\ newSends' = 1 /\ phase' = "DONE"
           /\ UNCHANGED <<oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
               rejected, cacheSeeded, retrySeeded, requestContext, requestPayload,
               decision, retryOf, reusedIdentity>>

FinishWithoutSend == /\ phase = "DECIDED" /\ decision \in {"REJECT", "REPLAY"}
                     /\ phase' = "DONE"
                     /\ UNCHANGED <<oldContext, oldPayload, oldState, oldHttp, oldAck, oldSends,
                         rejected, cacheSeeded, retrySeeded, requestContext, requestPayload,
                         decision, retryOf, newIdentity, newSends, reusedIdentity>>

Next == SendOld \/ (\E r \in {"OK", "OTHER_SUCCESS", "HTTP_ERROR", "UNKNOWN"}: ObserveOld(r))
        \/ (\E ack \in BOOLEAN: SettleOld(ack)) \/ CrashOld \/ LoadRecovery
        \/ (\E c \in Contexts, p \in Payloads: Request(c, p))
        \/ Decide \/ SendNew \/ FinishWithoutSend

TypeOK == /\ phase \in {"OLD_READY", "OLD_SENT", "OLD_OBSERVED", "FROZEN", "LOADED",
                        "REQUESTED", "DECIDED", "DONE"}
          /\ oldContext \in Contexts /\ oldPayload \in Payloads /\ oldState \in States
          /\ oldHttp \in {"NONE", "OK", "OTHER_SUCCESS", "HTTP_ERROR", "UNKNOWN"} /\ oldAck \in BOOLEAN
          /\ oldSends \in 0..1 /\ newSends \in 0..1 /\ newIdentity \in 0..2
          /\ retryOf \in 0..1 /\ reusedIdentity \in 0..1
          /\ requestContext \in Contexts \cup {"none"}
          /\ requestPayload \in Payloads \cup {"none"}
          /\ decision \in {"none", "REJECT", "REPLAY", "SEND"}
          /\ \A value \in {rejected, cacheSeeded, retrySeeded}: value \in BOOLEAN
UnresolvedRecoveryRejected == phase \in {"LOADED", "REQUESTED", "DECIDED", "DONE"}
    /\ Unresolved => rejected /\ ~cacheSeeded /\ ~retrySeeded
Non200CompleteRejected == phase \in {"LOADED", "REQUESTED", "DECIDED", "DONE"}
    /\ oldState = "COMPLETE" /\ oldHttp = "OTHER_SUCCESS" => rejected
NoUnknownResend == newSends > 0 => ~Unresolved
ReuseOnlyExactCommittedResponse == decision = "REPLAY" =>
    RequestKey = OldKey /\ oldState = "COMPLETE" /\ oldHttp = "OK" /\ oldAck /\ newSends = 0
RetryHasExactTerminalOrigin == retryOf = 1 =>
    RequestKey = OldKey /\ oldState = "ERROR" /\ oldHttp = "HTTP_ERROR" /\ oldAck
FreshPhysicalRetry == newSends > 0 => newIdentity = 2
RejectedDoesNotSend == rejected => newSends = 0
NeverReusedComplete == ~(phase = "DONE" /\ decision = "REPLAY")
NeverRetriedHttpError == ~(phase = "DONE" /\ retryOf = 1 /\ newIdentity = 2)
NeverRejectedKnownUnconfirmed == ~(phase = "DONE" /\ decision = "REJECT"
    /\ oldHttp = "OK" /\ oldState = "COMPLETE" /\ ~oldAck)
NeverRejectedUnknown == ~(phase = "DONE" /\ decision = "REJECT" /\ oldState = "UNKNOWN")
Spec == Init /\ [][Next]_vars
=============================================================================
