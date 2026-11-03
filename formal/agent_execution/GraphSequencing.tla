--------------------------- MODULE GraphSequencing ---------------------------
EXTENDS Naturals, TLC

CONSTANTS CheckCorrelation, CheckRoute, CheckPending
MaxEffects == 3
Nodes == {"calculate", "publish"}
Routes(n) == IF n = "calculate" THEN {"again", "done"} ELSE {"done"}
Target(n, r) == IF n = "calculate" /\ r = "again" THEN "calculate"
               ELSE IF n = "calculate" /\ r = "done" THEN "publish"
               ELSE "complete"
VARIABLES s, correlated, declaredRoute, noReplay, terminalStable
vars == <<s, correlated, declaredRoute, noReplay, terminalStable>>

Init ==
    /\ s = [node |-> "calculate", pending |-> 0, sequence |-> 0]
    /\ correlated = TRUE /\ declaredRoute = TRUE
    /\ noReplay = TRUE /\ terminalStable = TRUE
Close(a) == [a EXCEPT !.node = "closed", !.pending = 0]
Issue ==
    /\ s.node # "closed"
    (* State-space bound, not a production operation limit. *)
    /\ s.sequence < MaxEffects \/ s.pending # 0 \/ s.node = "complete"
    /\ LET next ==
          IF CheckPending /\ s.pending # 0 THEN Close(s)
          ELSE IF s.node = "complete" THEN s
          ELSE [s EXCEPT !.sequence = @ + 1, !.pending = s.sequence + 1]
       IN /\ s' = next
          /\ noReplay' = (s.pending # 0 => next.node = "closed" /\ next.sequence = s.sequence)
          /\ terminalStable' = (s.node = "complete" => next = s)
    /\ UNCHANGED <<correlated, declaredRoute>>
Accept(id, route) ==
    /\ s.node # "closed"
    /\ LET match == s.pending # 0 /\ s.pending = id
           validRoute == s.node \in Nodes /\ route \in Routes(s.node)
           advance == s.pending # 0 /\ (match \/ ~CheckCorrelation)
                      /\ (validRoute \/ ~CheckRoute)
           next == IF advance THEN [s EXCEPT !.node = Target(s.node, route), !.pending = 0]
                   ELSE Close(s)
       IN /\ s' = next
          /\ correlated' = (advance => match)
          /\ declaredRoute' = (advance => validRoute)
    /\ UNCHANGED <<noReplay, terminalStable>>
CloseHandle ==
    /\ s.node # "closed" /\ s' = Close(s)
    /\ UNCHANGED <<correlated, declaredRoute, noReplay, terminalStable>>
Next == Issue \/ CloseHandle \/
    (\E id \in 0..4, route \in {"again", "done", "undeclared"} : Accept(id, route))
Spec == Init /\ [][Next]_vars
TypeOK == s \in [node : Nodes \cup {"complete", "closed"}, pending : 0..MaxEffects,
                 sequence : 0..MaxEffects]
PendingIdentity == s.pending # 0 => s.pending = s.sequence /\ s.node \in Nodes
SettlementCorrelated == correlated
DeclaredRouteOnly == declaredRoute
IssuedEffectNotReplayed == noReplay
CompleteNextStable == terminalStable
=============================================================================
