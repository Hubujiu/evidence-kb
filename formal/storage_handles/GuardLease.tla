----------------------------- MODULE GuardLease -----------------------------
EXTENDS Naturals
CONSTANTS CleanupFailedHeartbeat, RejectStoppedPing
VARIABLES client, heartbeat, held, stopped, outcome, now, deadline, pings,
          cleanupAttempted, failure, resumedAfterStop
vars == <<client, heartbeat, held, stopped, outcome, now, deadline, pings,
          cleanupAttempted, failure, resumedAfterStop>>
Init == /\ client = "opening" /\ heartbeat = "absent" /\ held = TRUE
        /\ stopped = FALSE /\ outcome = "none" /\ now = 0 /\ deadline = 2
        /\ pings = 0 /\ cleanupAttempted = FALSE /\ failure = FALSE /\ resumedAfterStop = FALSE
Start == /\ client = "opening" /\ heartbeat' = "running" /\ client' = "body"
         /\ UNCHANGED <<held, stopped, outcome, now, deadline, pings, cleanupAttempted,
                        failure, resumedAfterStop>>
StartFailure == /\ client = "opening" /\ heartbeat' = "failed" /\ failure' = TRUE
                /\ client' = IF CleanupFailedHeartbeat THEN "finishing" ELSE "closed"
                /\ UNCHANGED <<held, stopped, outcome, now, deadline, pings, cleanupAttempted,
                               resumedAfterStop>>
Tick == /\ now < 4 /\ now' = now + 1
        /\ UNCHANGED <<client, heartbeat, held, stopped, outcome, deadline, pings,
                       cleanupAttempted, failure, resumedAfterStop>>
Ping == /\ client = "body" /\ heartbeat = "running" /\ pings = 0
        /\ (~RejectStoppedPing \/ ~stopped)
        /\ (~stopped => now < deadline)
        /\ pings' = pings + 1 /\ deadline' = now + 2
        /\ resumedAfterStop' = stopped
        /\ UNCHANGED <<client, heartbeat, held, stopped, outcome, now,
                       cleanupAttempted, failure>>
Expire == /\ held /\ now >= deadline /\ ~stopped
          /\ held' = FALSE /\ stopped' = TRUE /\ outcome' = "ROLLED_BACK"
          /\ UNCHANGED <<client, heartbeat, now, deadline, pings, cleanupAttempted,
                         failure, resumedAfterStop>>
LeaveBody == /\ client = "body" /\ client' = "finishing"
             /\ UNCHANGED <<heartbeat, held, stopped, outcome, now, deadline, pings,
                            cleanupAttempted, failure, resumedAfterStop>>
Finish == /\ client = "finishing" /\ client' = "closed" /\ cleanupAttempted' = TRUE
          /\ held' = FALSE /\ stopped' = TRUE
          /\ outcome' = IF outcome = "none" THEN "RELEASED" ELSE outcome
          /\ UNCHANGED <<heartbeat, now, deadline, pings, failure, resumedAfterStop>>
LostFinish == /\ client = "finishing" /\ client' = "closed" /\ cleanupAttempted' = TRUE
              /\ held' \in {held, FALSE} /\ outcome' = "UNKNOWN"
              /\ stopped' = (stopped \/ ~held')
              /\ UNCHANGED <<heartbeat, now, deadline, pings, failure, resumedAfterStop>>
Next == Start \/ StartFailure \/ Tick \/ Ping \/ Expire \/ LeaveBody \/ Finish \/ LostFinish
Spec == Init /\ [][Next]_vars
TypeOK == /\ client \in {"opening", "body", "finishing", "closed"}
          /\ heartbeat \in {"absent", "running", "failed"}
          /\ held \in BOOLEAN /\ stopped \in BOOLEAN /\ outcome \in {"none", "ROLLED_BACK", "RELEASED", "UNKNOWN"}
          /\ now \in 0..4 /\ deadline \in 2..6 /\ pings \in 0..1
          /\ cleanupAttempted \in BOOLEAN /\ failure \in BOOLEAN /\ resumedAfterStop \in BOOLEAN
FailedStartStillCleans == heartbeat = "failed" /\ client = "closed" => cleanupAttempted
NoStoppedPing == ~resumedAfterStop
ReleaseNeedsActualRelease == outcome = "RELEASED" => ~held /\ stopped
FailurePreserved == heartbeat = "failed" => failure
NoRenewedLease == ~(pings = 1 /\ deadline > 2)
NoFailedStartRelease == ~(heartbeat = "failed" /\ outcome = "RELEASED")
=============================================================================
