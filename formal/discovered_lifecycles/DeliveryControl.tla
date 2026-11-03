--------------------------- MODULE DeliveryControl -------------------------
EXTENDS Naturals
CONSTANTS EarlyRelease, IgnoreIdentity, IgnoreMarker, IgnoreInstance
VARIABLE s
vars == <<s>>
Init == s = [phase |-> "suspended", handle |-> FALSE, receipt |-> FALSE,
             released |-> FALSE, unsafeRelease |-> FALSE, observed |-> FALSE,
             falseLiveness |-> FALSE, launchUnknown |-> FALSE, exited |-> FALSE,
             stop |-> "idle", sameProcess |-> FALSE, marker |-> FALSE,
             unsafeMarker |-> FALSE, timedOut |-> FALSE]
Acquire == /\ s.phase = "suspended" /\ ~s.handle
           /\ s' = [s EXCEPT !.handle = TRUE]
Publish == /\ s.phase = "suspended" /\ s.handle /\ ~s.receipt
           /\ s' = [s EXCEPT !.receipt = TRUE]
Release == /\ s.phase = "suspended" /\ (s.receipt \/ EarlyRelease)
           /\ s' = [s EXCEPT !.phase = "running", !.released = TRUE,
                      !.unsafeRelease = (~s.receipt \/ ~s.handle)]
LaunchFailure == /\ s.phase = "suspended"
                 /\ s' = [s EXCEPT !.phase = "failed", !.launchUnknown = s.receipt]
Observe(matches) == /\ s.phase = "running" /\ ~s.observed
                    /\ (matches \/ IgnoreInstance)
                    /\ s' = [s EXCEPT !.observed = TRUE, !.falseLiveness = ~matches]
Exit == /\ s.phase \in {"running", "failed"} /\ ~s.exited
        /\ s' = [s EXCEPT !.exited = TRUE]
StartStop(same) == /\ s.receipt /\ s.stop = "idle"
                  /\ s' = [s EXCEPT !.stop = "checking", !.sameProcess = same]
StopMarker(valid) == /\ s.stop = "checking" /\ ~s.exited
                    /\ (s.sameProcess \/ IgnoreIdentity) /\ (valid \/ IgnoreMarker)
                    /\ s' = [s EXCEPT !.stop = "waiting", !.marker = TRUE,
                               !.unsafeMarker = (~s.sameProcess \/ ~valid)]
RefuseStop == /\ s.stop = "checking" /\ ~s.sameProcess
              /\ s' = [s EXCEPT !.stop = "refused"]
AlreadyExited == /\ s.stop \in {"checking", "waiting"} /\ s.exited
                 /\ s' = [s EXCEPT !.stop = "done"]
Timeout == /\ s.stop = "waiting" /\ ~s.exited
           /\ s' = [s EXCEPT !.stop = "timeout", !.timedOut = TRUE]
Next == Acquire \/ Publish \/ Release \/ LaunchFailure \/ Exit \/ RefuseStop \/ AlreadyExited \/ Timeout
        \/ (\E matching \in BOOLEAN: Observe(matching) \/ StartStop(matching) \/ StopMarker(matching))
Spec == Init /\ [][Next]_vars
TypeOK == /\ s.phase \in {"suspended", "running", "failed"}
          /\ s.stop \in {"idle", "checking", "waiting", "refused", "done", "timeout"}
ReleaseRequiresReceipt == ~s.unsafeRelease
MarkerRequiresIdentity == ~s.unsafeMarker
ExactInstanceLiveness == ~s.falseLiveness
WitnessLaunched == ~(s.released /\ s.receipt /\ s.handle /\ s.observed)
WitnessUnknownLaunch == ~(s.launchUnknown /\ s.receipt /\ s.phase = "failed")
WitnessStaleRefusal == ~(s.stop = "refused" /\ ~s.marker)
WitnessStopTimeout == ~(s.timedOut /\ s.marker /\ ~s.exited)
WitnessStopped == ~(s.stop = "done" /\ s.marker /\ s.exited)
=============================================================================
