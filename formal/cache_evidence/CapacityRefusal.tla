------------------------- MODULE CapacityRefusal -------------------------
EXTENDS Naturals
CONSTANT UnknownRefusal
VARIABLES slots, phase, dispatches, reported, eventAttempts
vars == <<slots, phase, dispatches, reported, eventAttempts>>
Init == /\ slots \in 0..2 /\ phase = "new"
        /\ dispatches = 0 /\ reported = "missing" /\ eventAttempts = 0
Admit == /\ phase = "new" /\ slots < 2
         /\ phase' = "invoked" /\ slots' = slots + 1 /\ dispatches' = 1
         /\ UNCHANGED <<reported, eventAttempts>>
Refuse == /\ phase = "new" /\ slots = 2
          /\ phase' = "refused"
          /\ reported' = IF UnknownRefusal THEN "missing" ELSE "zero"
          /\ UNCHANGED <<slots, dispatches, eventAttempts>>
Observe == /\ phase = "refused"
           /\ phase' = "observed"
           /\ eventAttempts' = IF reported = "zero" THEN 0 ELSE 1
           /\ UNCHANGED <<slots, dispatches, reported>>
Next == Admit \/ Refuse \/ Observe
Spec == Init /\ [][Next]_vars
TypeOK == /\ slots \in 0..2 /\ phase \in {"new", "invoked", "refused", "observed"}
          /\ dispatches \in 0..1 /\ reported \in {"missing", "zero"} /\ eventAttempts \in 0..1
RefusalNeverDispatches == phase \in {"refused", "observed"} => dispatches = 0
RefusalReportsNoAttempt == phase = "observed" => eventAttempts = dispatches
=============================================================================
