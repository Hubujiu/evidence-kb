----------------------------- MODULE NativeSessions -----------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS Capacity, Mutation
VARIABLES expired, failed, removed, phase, stale, next, issued, finished, admitted, violation
vars == <<expired, failed, removed, phase, stale, next, issued, finished, admitted, violation>>
Handles == 1..3
Live == {h \in Handles: phase[h] # "none"}
Init == /\ expired = {} /\ failed = {} /\ removed = {} /\ phase = [h \in Handles |-> "none"] /\ stale = {}
        /\ next = 0 /\ issued = {} /\ finished = {} /\ admitted = {}
        /\ violation = FALSE
Open(k) == /\ stale = {} /\ next < 3 /\ (Cardinality(Live) < Capacity \/ Mutation = "capacity")
           /\ LET h == IF Mutation = "reuse" /\ next > 0 THEN 1 ELSE next + 1
              IN /\ phase' = [phase EXCEPT ![h] = k]
                 /\ violation' = (violation \/ h \in issued)
                 /\ issued' = issued \cup {h}
           /\ next' = next + 1
           /\ UNCHANGED <<expired,failed,removed,stale,finished,admitted>>
Age(h) == /\ h \in Live /\ h \notin stale /\ stale' = stale \cup {h}
          /\ UNCHANGED <<expired,failed,removed,phase,next,issued,finished,admitted,violation>>
Sweep == /\ stale # {} /\ phase' = [h \in Handles |-> IF h \in stale THEN "none" ELSE phase[h]]
         /\ stale' = {} /\ expired' = expired \cup stale
         /\ UNCHANGED <<failed,removed,next,issued,finished,admitted,violation>>
Admit(h) == /\ phase[h] = "input" /\ h \notin stale
            /\ phase' = [phase EXCEPT ![h] = "executing"]
            /\ admitted' = admitted \cup {h}
            /\ UNCHANGED <<expired,failed,removed,stale,next,issued,finished,violation>>
Result(h) == /\ h \notin stale /\ (phase[h] = "executing" \/ (Mutation = "admission" /\ phase[h] = "input"))
             /\ phase' = [phase EXCEPT ![h] = "result"]
             /\ UNCHANGED <<expired,failed,removed,stale,next,issued,finished,admitted,violation>>
Fail(h) == /\ h \in Live /\ h \notin stale /\ phase[h] # "failed"
           /\ phase' = [phase EXCEPT ![h] = "failed"]
           /\ failed' = failed \cup {h}
           /\ UNCHANGED <<expired,removed,stale,next,issued,finished,admitted,violation>>
Finish(h) == /\ h \in Live /\ h \notin stale
             /\ phase' = [phase EXCEPT ![h] = IF Mutation = "finish" THEN @ ELSE "none"]
             /\ finished' = finished \cup {h}
             /\ removed' = removed \cup {h}
             /\ UNCHANGED <<expired,failed,stale,next,issued,admitted,violation>>
Close(h) == /\ h \in Live /\ phase' = [phase EXCEPT ![h] = "none"]
            /\ stale' = stale \ {h} /\ removed' = removed \cup {h}
            /\ UNCHANGED <<expired,failed,next,issued,finished,admitted,violation>>
Next == (\E k \in {"input","values"}: Open(k)) \/ Sweep
        \/ (\E h \in Handles: Age(h) \/ Admit(h) \/ Result(h) \/ Fail(h) \/ Finish(h) \/ Close(h))
TypeOK == /\ expired \subseteq Handles /\ failed \subseteq Handles /\ removed \subseteq Handles
          /\ phase \in [Handles -> {"none","input","values","executing","result","failed"}]
          /\ stale \subseteq Handles /\ next \in 0..3 /\ issued \subseteq Handles
          /\ finished \subseteq Handles /\ admitted \subseteq Handles /\ violation \in BOOLEAN
Bounded == Cardinality(Live) <= Capacity
NoReuse == ~violation
NoExecuteBeforeAdmission == \A h \in Handles: phase[h] = "result" => h \in admitted
FinishConsumes == Live \intersect finished = {}
WitnessValues == ~(\E h \in issued: h \in finished /\ h \notin admitted)
WitnessOverlap == ~(Cardinality(Live) = 2)
WitnessExpiry == ~(expired # {} /\ expired \intersect Live = {})
WitnessFailedCleanup == ~(failed \intersect removed # {} /\ failed \intersect removed \intersect Live = {})
Spec == Init /\ [][Next]_vars
=============================================================================
