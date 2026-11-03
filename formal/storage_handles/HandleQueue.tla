----------------------------- MODULE HandleQueue -----------------------------
EXTENDS Naturals
CONSTANT AtomicDrainClaim
VARIABLES worker, future, queued, stopped, registered, reports, executed, cancelledBeforeRun, timedOut
vars == <<worker, future, queued, stopped, registered, reports, executed, cancelledBeforeRun, timedOut>>
Init == /\ worker = "active" /\ future = "pending" /\ queued = TRUE
        /\ stopped = FALSE /\ registered = TRUE /\ reports = 0
        /\ executed = FALSE /\ cancelledBeforeRun = FALSE /\ timedOut = FALSE
Timeout == /\ ~timedOut
           /\ stopped' = TRUE /\ timedOut' = TRUE
           /\ future' = IF future = "pending" THEN "cancelled" ELSE future
           /\ cancelledBeforeRun' = (future = "pending")
           /\ UNCHANGED <<worker, queued, registered, reports, executed>>
Dispatch == /\ worker = "active" /\ ~stopped /\ queued /\ future = "pending"
            /\ worker' = "running" /\ future' = "running" /\ queued' = FALSE /\ executed' = TRUE
            /\ UNCHANGED <<stopped, registered, reports, cancelledBeforeRun, timedOut>>
Complete == /\ worker = "running" /\ worker' = "active" /\ future' = "completed"
            /\ UNCHANGED <<queued, stopped, registered, reports, executed, cancelledBeforeRun, timedOut>>
FinishActive == /\ worker = "active" /\ worker' = "drain" /\ stopped' = TRUE
                /\ UNCHANGED <<future, queued, registered, reports, executed, cancelledBeforeRun, timedOut>>
Drain == /\ worker = "drain"
         /\ worker' = IF ~queued \/ future = "cancelled" THEN "release" ELSE "publish"
         /\ future' = IF queued /\ future = "pending" /\ AtomicDrainClaim THEN "running" ELSE future
         /\ queued' = FALSE
         /\ UNCHANGED <<stopped, registered, reports, executed, cancelledBeforeRun, timedOut>>
Publish == /\ worker = "publish"
           /\ worker' = IF future = "cancelled" THEN "aborted" ELSE "release"
           /\ future' = IF future = "cancelled" THEN future ELSE "closed"
           /\ UNCHANGED <<queued, stopped, registered, reports, executed, cancelledBeforeRun, timedOut>>
Release == /\ worker = "release" /\ worker' = "done" /\ registered' = FALSE
           /\ stopped' = TRUE /\ reports' = reports + 1
           /\ UNCHANGED <<future, queued, executed, cancelledBeforeRun, timedOut>>
Next == Timeout \/ Dispatch \/ Complete \/ FinishActive \/ Drain \/ Publish \/ Release
Spec == Init /\ [][Next]_vars
TypeOK == /\ worker \in {"active", "running", "drain", "publish", "release", "done", "aborted"}
          /\ future \in {"pending", "cancelled", "running", "completed", "closed"}
          /\ queued \in BOOLEAN /\ stopped \in BOOLEAN /\ registered \in BOOLEAN
          /\ executed \in BOOLEAN /\ cancelledBeforeRun \in BOOLEAN /\ timedOut \in BOOLEAN /\ reports \in 0..1
DrainCannotAbort == worker # "aborted"
CancelledNeverExecutes == cancelledBeforeRun => ~executed
NoPrematureRetirement == worker # "done" => registered
TerminalOwnerRetired == worker = "done" => ~registered /\ reports = 1
NoFreshAction == ~executed
NoCancelledDrain == ~(worker = "done" /\ cancelledBeforeRun)
=============================================================================
