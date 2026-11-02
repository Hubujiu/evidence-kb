--------------------------- MODULE LivenessControl ---------------------------
EXTENDS Naturals
CONSTANT Advance
VARIABLE phase
Init == phase = 0
Next == phase' = IF Advance THEN 1 ELSE phase
TypeOK == phase \in {0, 1}
EventuallyActive == <>(phase = 1)
Spec == Init /\ [][Next]_phase /\ WF_phase(Next)
=============================================================================
