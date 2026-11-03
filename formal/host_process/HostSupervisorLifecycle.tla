------------------------ MODULE HostSupervisorLifecycle ------------------------
EXTENDS Naturals
CONSTANTS SkipStopWait, IgnoreCleanupError
VARIABLES phase, run, stop, shutdown, runReply, runResponse, shutdownReply, root,
          reaped, cleanup, result
vars == <<phase, run, stop, shutdown, runReply, runResponse, shutdownReply, root,
          reaped, cleanup, result>>
Init == /\ phase = "new" /\ run = "idle" /\ stop = "idle"
        /\ shutdown = FALSE /\ runReply = "none" /\ runResponse = "none" /\ shutdownReply = FALSE
        /\ root = FALSE /\ reaped = FALSE /\ cleanup = "none" /\ result = "none"
Boot == /\ phase \in {"new", "handshaken"}
        /\ phase' = IF phase = "new" THEN "handshaken" ELSE "ready"
        /\ root' = (phase = "handshaken")
        /\ UNCHANGED <<runResponse, run, stop, shutdown, runReply, shutdownReply, reaped, cleanup, result>>
StartRun == /\ phase = "ready" /\ phase' = "running" /\ run' = "running"
            /\ UNCHANGED <<stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup, result>>
ClaimStop == /\ phase = "running" /\ ~shutdown /\ shutdown' = TRUE
             /\ stop' = IF run = "running" THEN "busy" ELSE "done"
             /\ UNCHANGED <<phase, run, runReply, runResponse, shutdownReply, root, reaped, cleanup, result>>
StopDone == /\ stop = "busy" /\ stop' = "done"
            /\ UNCHANGED <<phase, run, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup, result>>
RunReturns == /\ run = "running" /\ run' = "returned"
              /\ UNCHANGED <<phase, stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup, result>>
BeginClose == /\ run = "returned" /\ (stop # "busy" \/ SkipStopWait)
              /\ run' = "closing"
              /\ UNCHANGED <<phase, stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup, result>>
CloseReturns == /\ run = "closing" /\ run' \in {"closed", "closeError"}
                /\ UNCHANGED <<phase, stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup, result>>
EmitRunReply == /\ run \in {"closed", "closeError"} /\ runResponse = "none"
                /\ runResponse' = IF run = "closed" THEN "ok" ELSE "error"
                /\ UNCHANGED <<phase, run, stop, shutdown, runReply, shutdownReply, root, reaped, cleanup, result>>
SettleRun == /\ run \in {"closed", "closeError"} /\ runResponse # "none" /\ run' = "settled"
             /\ UNCHANGED <<phase, stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup, result>>
RunAcknowledged == /\ runResponse # "none" /\ runReply = "none" /\ runReply' = runResponse
                   /\ UNCHANGED <<phase, run, stop, shutdown, runResponse, shutdownReply, root, reaped, cleanup, result>>
ShutdownAcknowledged == /\ shutdown /\ run = "settled" /\ stop = "done"
                        /\ ~shutdownReply /\ shutdownReply' = TRUE
                        /\ UNCHANGED <<runResponse, phase, run, stop, shutdown, runReply, root, reaped, cleanup, result>>
CollectRoot == /\ root /\ run \in {"closing", "closed", "closeError", "settled"}
               /\ root' = FALSE
               /\ UNCHANGED <<runResponse, phase, run, stop, shutdown, runReply, shutdownReply, reaped, cleanup, result>>
Reap == /\ shutdownReply /\ ~reaped /\ reaped' = TRUE
        /\ UNCHANGED <<phase, run, stop, shutdown, runReply, runResponse, shutdownReply, root, cleanup, result>>
Cleanup == /\ cleanup = "none" /\ reaped /\ cleanup' \in {"ack", "error"}
           /\ UNCHANGED <<phase, run, stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, result>>
Publish == /\ result = "none" /\ runReply = "ok" /\ shutdownReply /\ ~root /\ reaped
           /\ (cleanup = "ack" \/ (IgnoreCleanupError /\ cleanup = "error"))
           /\ result' = "completed"
           /\ UNCHANGED <<phase, run, stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup>>
Fail == /\ result = "none" /\ result' = "error"
        /\ UNCHANGED <<phase, run, stop, shutdown, runReply, runResponse, shutdownReply, root, reaped, cleanup>>
Next == Boot \/ StartRun \/ ClaimStop \/ StopDone \/ RunReturns \/ BeginClose \/ CloseReturns
        \/ EmitRunReply \/ SettleRun \/ RunAcknowledged \/ ShutdownAcknowledged \/ CollectRoot \/ Reap \/ Cleanup \/ Publish \/ Fail
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"new", "handshaken", "ready", "running"}
          /\ run \in {"idle", "running", "returned", "closing", "closed", "closeError", "settled"}
          /\ stop \in {"idle", "busy", "done"} /\ shutdown \in BOOLEAN
          /\ runReply \in {"none", "ok", "error"} /\ runResponse \in {"none", "ok", "error"} /\ shutdownReply \in BOOLEAN
          /\ root \in BOOLEAN /\ reaped \in BOOLEAN /\ cleanup \in {"none", "ack", "error"}
          /\ result \in {"none", "completed", "error"}
NoConcurrentStopClose == run \in {"closing", "closed", "closeError", "settled"} => stop # "busy"
ShutdownWaitsForSettlement == shutdownReply => run = "settled" /\ stop = "done"
SuccessRequiresCleanup == result = "completed" =>
    runReply = "ok" /\ shutdownReply /\ ~root /\ reaped /\ cleanup = "ack"
=============================================================================
