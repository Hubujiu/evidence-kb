------------------------------ MODULE PageCache ------------------------------
EXTENDS Naturals
CONSTANTS PreservePartial, CheckIdentity
VARIABLES phase, payload, cached, observed, identity, admitted
vars == <<phase, payload, cached, observed, identity, admitted>>
Outcomes == {"SUCCESS", "PARTIAL_SUCCESS"}
Init == /\ phase = "new" /\ payload = "none" /\ cached = "none"
        /\ observed = "none" /\ identity = "none" /\ admitted = FALSE
Parse(outcome, source) == /\ phase = "new" /\ outcome \in Outcomes
                         /\ source \in {"original", "wrong"}
                         /\ payload' = outcome /\ identity' = source
                         /\ phase' = "parsed"
                         /\ UNCHANGED <<cached, observed, admitted>>
Accept == /\ phase = "parsed"
          /\ admitted' = (~CheckIdentity \/ identity = "original")
          /\ observed' = IF ~CheckIdentity \/ identity = "original" THEN payload ELSE "FAILED"
          /\ cached' = IF ~CheckIdentity \/ identity = "original" THEN payload ELSE "none"
          /\ phase' = "cold"
          /\ UNCHANGED <<payload, identity>>
Reuse == /\ phase = "cold" /\ cached # "none"
         /\ observed' = IF PreservePartial THEN cached ELSE "SUCCESS"
         /\ phase' = "warm"
         /\ UNCHANGED <<payload, cached, identity, admitted>>
Next == (\E outcome \in Outcomes, source \in {"original", "wrong"} : Parse(outcome, source)) \/ Accept \/ Reuse
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"new", "parsed", "cold", "warm"}
          /\ payload \in Outcomes \cup {"none"} /\ cached \in Outcomes \cup {"none"}
          /\ observed \in Outcomes \cup {"none", "FAILED"}
          /\ identity \in {"none", "original", "wrong"} /\ admitted \in BOOLEAN
NoFalseReady == observed = "SUCCESS" => payload = "SUCCESS"
IdentityBound == admitted => identity = "original"
WarmPartialUnreachable == ~(phase = "warm" /\ observed = "PARTIAL_SUCCESS")
=============================================================================
