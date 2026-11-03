---------------------------- MODULE GateControl ----------------------------
EXTENDS Naturals
CONSTANT SeedBug
VARIABLE phase
vars == <<phase>>
Init == phase = "idle"
Next == \/ /\ phase = "idle"
           /\ phase' = IF SeedBug THEN "released" ELSE "active"
        \/ /\ phase = "active"
           /\ UNCHANGED vars
TypeOK == phase \in {"idle", "active", "released"}
NoPrematureRelease == phase # "released"
Spec == Init /\ [][Next]_vars
=============================================================================
