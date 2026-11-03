--------------------------- MODULE PythonPump ---------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS SingleOwner, ReleaseOwner, RetainFailure, CollectAfterDone
VARIABLES owners, pc, phase, resource, attempts, sealed, collected, snapshot, failed
vars == <<owners,pc,phase,resource,attempts,sealed,collected,snapshot,failed>>
Init == /\ owners = {} /\ pc = [t \in 1..2 |-> "idle"] /\ phase = "init"
        /\ resource = FALSE /\ attempts = 0 /\ sealed = FALSE
        /\ collected = FALSE /\ snapshot = FALSE /\ failed = FALSE
Claim(t) == /\ pc[t] = "idle" /\ ~collected /\ (~SingleOwner \/ owners = {})
            /\ owners' = owners \cup {t} /\ pc' = [pc EXCEPT ![t] = "poll"]
            /\ UNCHANGED <<phase,resource,attempts,sealed,collected,snapshot,failed>>
InitCallback(t) == /\ t \in owners /\ pc[t] = "poll" /\ phase = "init"
                   /\ phase' = "active" /\ sealed' = TRUE /\ resource' = TRUE
                   /\ pc' = [pc EXCEPT ![t] = "release"]
                   /\ UNCHANGED <<owners,attempts,collected,snapshot,failed>>
Drain == /\ phase = "active" /\ phase' = "cleanup"
         /\ UNCHANGED <<owners,pc,resource,attempts,sealed,collected,snapshot,failed>>
Cleanup(t, ok) == /\ t \in owners /\ pc[t] = "poll" /\ phase = "cleanup"
                  /\ ok \in BOOLEAN /\ attempts < 2
                  /\ attempts' = attempts + 1 /\ failed' = ~ok
                  /\ resource' = (IF ok \/ ~RetainFailure THEN FALSE ELSE resource)
                  /\ phase' = (IF ok THEN "disposed" ELSE "failed")
                  /\ pc' = [pc EXCEPT ![t] = "release"]
                  /\ UNCHANGED <<owners,sealed,collected,snapshot>>
Retry == /\ phase = "failed" /\ attempts < 2 /\ phase' = "cleanup"
         /\ UNCHANGED <<owners,pc,resource,attempts,sealed,collected,snapshot,failed>>
EmptyPoll(t) == /\ t \in owners /\ pc[t] = "poll" /\ phase \in {"active","failed","disposed"}
                /\ pc' = [pc EXCEPT ![t] = "release"]
                /\ UNCHANGED <<owners,phase,resource,attempts,sealed,collected,snapshot,failed>>
Release(t) == /\ pc[t] = "release" /\ pc' = [pc EXCEPT ![t] = "idle"]
              /\ owners' = (IF ReleaseOwner THEN owners \ {t} ELSE owners)
              /\ UNCHANGED <<phase,resource,attempts,sealed,collected,snapshot,failed>>
Collect == /\ ~collected /\ (~CollectAfterDone \/ phase = "disposed")
           /\ collected' = TRUE /\ snapshot' = TRUE
           /\ UNCHANGED <<owners,pc,phase,resource,attempts,sealed,failed>>
Next == (\E t \in 1..2 : Claim(t) \/ InitCallback(t) \/ EmptyPoll(t) \/ Release(t)
                        \/ (\E ok \in BOOLEAN : Cleanup(t,ok))) \/ Drain \/ Retry \/ Collect
Spec == Init /\ [][Next]_vars
TypeOK == /\ owners \subseteq 1..2 /\ pc \in [1..2 -> {"idle","poll","release"}]
          /\ phase \in {"init","active","cleanup","failed","disposed"} /\ attempts \in 0..2
          /\ {resource,sealed,collected,snapshot,failed} \subseteq BOOLEAN
SinglePump == Cardinality(owners) <= 1
NoIdleToken == \A t \in owners : pc[t] # "idle"
FailedCallbackRetained == phase = "failed" => resource
NoPrematureCollection == collected => phase = "disposed" /\ ~resource /\ sealed
RetryCollectedUnreachable == ~(collected /\ attempts = 2 /\ owners = {})
=============================================================================
