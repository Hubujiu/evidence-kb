------------------------ MODULE AcceptanceWorkflow ------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Owners, UseGuard, PreservePending
VARIABLES owner, pc, cached, receipt, effects, restarts
vars == <<owner, pc, cached, receipt, effects, restarts>>
ReceiptStates == {"ABSENT", "STARTED", "COMPLETE", "ERROR", "UNKNOWN"}
Active == {"read", "claim", "send", "observe", "release"}
Init == /\ owner = "none"
        /\ pc = [p \in Owners |-> "idle"]
        /\ cached = [p \in Owners |-> "ABSENT"]
        /\ receipt = "ABSENT"
        /\ effects = 0
        /\ restarts = [p \in Owners |-> 0]
Acquire(p) == /\ pc[p] = "idle"
              /\ ~UseGuard \/ owner = "none"
              /\ owner' = IF UseGuard THEN p ELSE owner
              /\ pc' = [pc EXCEPT ![p] = "read"]
              /\ UNCHANGED <<cached, receipt, effects, restarts>>
Read(p) == /\ pc[p] = "read"
           /\ cached' = [cached EXCEPT ![p] = receipt]
           /\ pc' = [pc EXCEPT ![p] =
                IF receipt = "ABSENT" \/ (~PreservePending /\ receipt \in {"STARTED", "UNKNOWN"})
                THEN "claim" ELSE "release"]
           /\ UNCHANGED <<owner, receipt, effects, restarts>>
Claim(p) == /\ pc[p] = "claim"
            /\ receipt' = "STARTED"
            /\ pc' = [pc EXCEPT ![p] = "send"]
            /\ UNCHANGED <<owner, cached, effects, restarts>>
Send(p) == /\ pc[p] = "send"
           /\ effects < Cardinality(Owners) * 2
           /\ effects' = effects + 1
           /\ pc' = [pc EXCEPT ![p] = "observe"]
           /\ UNCHANGED <<owner, cached, receipt, restarts>>
Observe(p, outcome) == /\ pc[p] = "observe"
                       /\ outcome \in {"COMPLETE", "ERROR", "UNKNOWN"}
                       /\ receipt' = outcome
                       /\ pc' = [pc EXCEPT ![p] = "release"]
                       /\ UNCHANGED <<owner, cached, effects, restarts>>
Release(p) == /\ pc[p] = "release"
              /\ owner' = IF owner = p THEN "none" ELSE owner
              /\ pc' = [pc EXCEPT ![p] = "done"]
              /\ UNCHANGED <<cached, receipt, effects, restarts>>
Crash(p) == /\ pc[p] \in Active
            /\ owner' = IF owner = p THEN "none" ELSE owner
            /\ pc' = [pc EXCEPT ![p] = "crashed"]
            /\ UNCHANGED <<cached, receipt, effects, restarts>>
Restart(p) == /\ pc[p] = "crashed" /\ restarts[p] = 0
              /\ pc' = [pc EXCEPT ![p] = "idle"]
              /\ restarts' = [restarts EXCEPT ![p] = 1]
              /\ UNCHANGED <<owner, cached, receipt, effects>>
Next == \E p \in Owners : Acquire(p) \/ Read(p) \/ Claim(p) \/ Send(p) \/
        (\E outcome \in {"COMPLETE", "ERROR", "UNKNOWN"} : Observe(p, outcome)) \/
        Release(p) \/ Crash(p) \/ Restart(p)
Spec == Init /\ [][Next]_vars
TypeOK == /\ owner \in Owners \cup {"none"}
          /\ pc \in [Owners -> Active \cup {"idle", "done", "crashed"}]
          /\ cached \in [Owners -> ReceiptStates]
          /\ receipt \in ReceiptStates
          /\ effects \in 0..(Cardinality(Owners) * 2)
          /\ restarts \in [Owners -> 0..1]
NoDuplicateEffect == effects <= 1
MarkerBeforeEffect == effects > 0 => receipt # "ABSENT"
ExclusiveOwner == Cardinality({p \in Owners : pc[p] \in Active}) <= 1
CompleteHasEffect == receipt = "COMPLETE" => effects > 0
=============================================================================
