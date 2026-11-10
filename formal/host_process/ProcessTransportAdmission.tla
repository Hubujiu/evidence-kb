------------------------- MODULE ProcessTransportAdmission -------------------------
EXTENDS TLC
CONSTANT BypassAdmission
VARIABLES spawned, running, draining, closed, spawnedAtDrain
vars == <<spawned, running, draining, closed, spawnedAtDrain>>

Init == /\ spawned = FALSE
        /\ running = FALSE
        /\ draining = FALSE
        /\ closed = FALSE
        /\ spawnedAtDrain = FALSE

Start == /\ ~spawned
         /\ (BypassAdmission \/ (~draining /\ ~closed))
         /\ spawned' = TRUE
         /\ running' = TRUE
         /\ UNCHANGED <<draining, closed, spawnedAtDrain>>

BeginClose == /\ ~draining
              /\ ~closed
              /\ draining' = TRUE
              /\ spawnedAtDrain' = spawned
              /\ UNCHANGED <<spawned, running, closed>>

StopProcess == /\ draining
               /\ running
               /\ running' = FALSE
               /\ UNCHANGED <<spawned, draining, closed, spawnedAtDrain>>

FinishClose == /\ draining
               /\ ~running
               /\ ~closed
               /\ closed' = TRUE
               /\ UNCHANGED <<spawned, running, draining, spawnedAtDrain>>

Next == Start \/ BeginClose \/ StopProcess \/ FinishClose
Spec == Init /\ [][Next]_vars
TypeOK == /\ spawned \in BOOLEAN
          /\ running \in BOOLEAN
          /\ draining \in BOOLEAN
          /\ closed \in BOOLEAN
          /\ spawnedAtDrain \in BOOLEAN
NoSpawnAfterDrain == draining => spawned = spawnedAtDrain
ClosedOwnsNoRunningProcess == closed => ~running
RunningHasOwner == running => spawned
ClosedRemainsDraining == closed => draining
===================================================================================
