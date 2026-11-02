----------------------- MODULE ProviderPhysicalKnowledge -----------------------
EXTENDS Naturals
CONSTANTS ExitMeansNotSent, RetryUnknown
VARIABLES attempt, stage, invocations, receipt, cancelled, joined, pluginClosed,
          processExited, readerStopped, writerStopped, transportClosed
vars == <<attempt, stage, invocations, receipt, cancelled, joined, pluginClosed,
          processExited, readerStopped, writerStopped, transportClosed>>
Init == /\ attempt = 1 /\ stage = "prepared" /\ invocations = 0 /\ receipt = "pending"
        /\ cancelled = FALSE /\ joined = FALSE /\ pluginClosed = FALSE
        /\ processExited = FALSE /\ readerStopped = FALSE /\ writerStopped = FALSE
        /\ transportClosed = FALSE
ClaimIntent == /\ stage = "prepared" /\ receipt = "pending" /\ stage' = "intent"
               /\ UNCHANGED <<attempt, invocations, receipt, cancelled, joined, pluginClosed,
                              processExited, readerStopped, writerStopped, transportClosed>>
WriteFrame == /\ stage = "intent" /\ receipt = "pending" /\ stage' = "wire"
              /\ UNCHANGED <<attempt, invocations, receipt, cancelled, joined, pluginClosed,
                             processExited, readerStopped, writerStopped, transportClosed>>
Invoke == /\ stage = "wire" /\ ~processExited
          /\ stage' = "invoked" /\ invocations' = invocations + 1
          /\ UNCHANGED <<attempt, receipt, cancelled, joined, pluginClosed,
                         processExited, readerStopped, writerStopped, transportClosed>>
Return == /\ stage = "invoked" /\ ~processExited /\ stage' = "returned"
          /\ UNCHANGED <<attempt, invocations, receipt, cancelled, joined, pluginClosed,
                         processExited, readerStopped, writerStopped, transportClosed>>
Observe == /\ stage = "returned" /\ receipt = "pending" /\ receipt' = "known"
           /\ UNCHANGED <<attempt, stage, invocations, cancelled, joined, pluginClosed,
                          processExited, readerStopped, writerStopped, transportClosed>>
Lose == /\ receipt = "pending"
        /\ receipt' = IF stage = "prepared" \/ ExitMeansNotSent THEN "notSent" ELSE "unknown"
        /\ UNCHANGED <<attempt, stage, invocations, cancelled, joined, pluginClosed,
                       processExited, readerStopped, writerStopped, transportClosed>>
Cancel == /\ ~cancelled /\ cancelled' = TRUE
          /\ UNCHANGED <<attempt, stage, invocations, receipt, joined, pluginClosed,
                         processExited, readerStopped, writerStopped, transportClosed>>
Join == /\ ~joined /\ ~processExited /\ stage \notin {"invoked", "wire"} /\ receipt # "pending" /\ joined' = TRUE
        /\ UNCHANGED <<attempt, stage, invocations, receipt, cancelled, pluginClosed,
                       processExited, readerStopped, writerStopped, transportClosed>>
ClosePlugin == /\ joined /\ ~processExited /\ ~pluginClosed /\ pluginClosed' = TRUE
               /\ UNCHANGED <<attempt, stage, invocations, receipt, cancelled, joined,
                              processExited, readerStopped, writerStopped, transportClosed>>
Exit == /\ ~processExited /\ receipt # "pending" /\ processExited' = TRUE
        /\ UNCHANGED <<attempt, stage, invocations, receipt, cancelled, joined, pluginClosed,
                       readerStopped, writerStopped, transportClosed>>
StopReader == /\ processExited /\ ~readerStopped /\ readerStopped' = TRUE
              /\ UNCHANGED <<attempt, stage, invocations, receipt, cancelled, joined, pluginClosed,
                             processExited, writerStopped, transportClosed>>
StopWriter == /\ processExited /\ ~writerStopped /\ writerStopped' = TRUE
              /\ UNCHANGED <<attempt, stage, invocations, receipt, cancelled, joined, pluginClosed,
                             processExited, readerStopped, transportClosed>>
CloseTransport == /\ processExited /\ readerStopped /\ writerStopped /\ ~transportClosed
                  /\ transportClosed' = TRUE
                  /\ UNCHANGED <<attempt, stage, invocations, receipt, cancelled, joined, pluginClosed,
                                 processExited, readerStopped, writerStopped>>
Retry == /\ RetryUnknown /\ receipt = "unknown" /\ attempt = 1 /\ transportClosed
         /\ attempt' = 2 /\ stage' = "prepared" /\ receipt' = "pending"
         /\ cancelled' = FALSE /\ joined' = FALSE /\ pluginClosed' = FALSE
         /\ processExited' = FALSE /\ readerStopped' = FALSE /\ writerStopped' = FALSE
         /\ transportClosed' = FALSE /\ UNCHANGED invocations
Next == ClaimIntent \/ WriteFrame \/ Invoke \/ Return \/ Observe \/ Lose \/ Cancel \/ Join
        \/ ClosePlugin \/ Exit \/ StopReader \/ StopWriter \/ CloseTransport \/ Retry
Spec == Init /\ [][Next]_vars
TypeOK == /\ attempt \in 1..2 /\ stage \in {"prepared", "intent", "wire", "invoked", "returned"}
          /\ invocations \in 0..2 /\ receipt \in {"pending", "known", "unknown", "notSent"}
          /\ cancelled \in BOOLEAN /\ joined \in BOOLEAN /\ pluginClosed \in BOOLEAN
          /\ processExited \in BOOLEAN /\ readerStopped \in BOOLEAN /\ writerStopped \in BOOLEAN
          /\ transportClosed \in BOOLEAN
NotSentMeansNoInvocation == receipt = "notSent" => invocations = 0
NoReplayAfterUnknown == attempt = 1
NoDuplicateInvocation == invocations <= 1
PluginCloseWaitsForJoin == pluginClosed => joined
TransportCloseWaitsForIo == transportClosed => processExited /\ readerStopped /\ writerStopped
=============================================================================
