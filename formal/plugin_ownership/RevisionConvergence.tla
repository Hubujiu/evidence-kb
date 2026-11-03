----------------------- MODULE RevisionConvergence -----------------------
EXTENDS Naturals, FiniteSets
CONSTANT PreservePendingClosure
VARIABLES desired, observed, current, target, affected, obligation, step,
          removed, noopSeen, carried, badObservation, independentChild,
          converged, unrelatedConverged, collectionConverged
vars == <<desired, observed, current, target, affected, obligation, step,
          removed, noopSeen, carried, badObservation, independentChild,
          converged, unrelatedConverged, collectionConverged>>
Owners == {"p", "q", "consumer"}
Local == {"p", "q"}
Closure(ids) == ids \cup (IF "p" \in ids THEN {"consumer"} ELSE {})
Settled(ids) == \A id \in ids \ removed : current[id] = target[id]
Init == /\ desired = 1 /\ observed = 1 /\ current = [id \in Owners |-> 1]
        /\ target = current /\ affected = {} /\ obligation = {} /\ step = 0
        /\ removed = {} /\ noopSeen = FALSE /\ carried = FALSE /\ badObservation = FALSE
        /\ independentChild = FALSE /\ converged = FALSE /\ unrelatedConverged = FALSE
        /\ collectionConverged = FALSE
FirstChange == /\ step = 0 /\ step' = 1 /\ desired' = 2
               /\ target' = [target EXCEPT !["p"] = 2, !["consumer"] = 2]
               /\ affected' = Closure({"p"}) /\ obligation' = Closure({"p"})
               /\ UNCHANGED <<observed, current, removed, noopSeen, carried, badObservation,
                              independentChild, converged, unrelatedConverged, collectionConverged>>
SecondChange == /\ step = 1 /\ step' = 2 /\ desired' = 3
                /\ target' = [target EXCEPT !["q"] = 2]
                /\ LET pending == IF observed # desired THEN affected ELSE {}
                   IN /\ affected' = Closure({"q"} \cup (IF PreservePendingClosure THEN pending ELSE {}))
                      /\ carried' = ("consumer" \in pending \cap affected')
                /\ obligation' = (IF observed # desired THEN obligation \cup {"q"} ELSE {"q"})
                /\ UNCHANGED <<observed, current, removed, noopSeen, badObservation,
                               independentChild, converged, unrelatedConverged, collectionConverged>>
NoopApply == /\ step \in {1,2} /\ ~noopSeen /\ noopSeen' = TRUE
             /\ UNCHANGED <<desired, observed, current, target, affected, obligation, step, removed,
                            carried, badObservation, independentChild, converged, unrelatedConverged, collectionConverged>>
Complete(id) == /\ id \in Owners \ removed /\ current[id] < target[id]
                /\ id = "consumer" => current["p"] = target["p"]
                /\ current' = [current EXCEPT ![id] = target[id]]
                /\ UNCHANGED <<desired, observed, target, affected, obligation, step, removed,
                               noopSeen, carried, badObservation, independentChild, converged,
                               unrelatedConverged, collectionConverged>>
CollectConsumer == /\ step = 2 /\ "consumer" \notin removed /\ ~independentChild
                   /\ removed' = {"consumer"} /\ affected' = affected \ {"consumer"}
                   /\ UNCHANGED <<desired, observed, current, target, obligation, step, noopSeen,
                                  carried, badObservation, independentChild, converged,
                                  unrelatedConverged, collectionConverged>>
Observe == /\ observed # desired /\ Settled(Local \cup affected)
           /\ observed' = desired /\ badObservation' = (badObservation \/ ~Settled(obligation))
           /\ converged' = (converged \/ (desired = 3 /\ carried /\ "consumer" \notin removed))
           /\ collectionConverged' = (collectionConverged \/ (desired = 3 /\ "consumer" \in removed /\ carried))
           /\ unrelatedConverged' = (unrelatedConverged \/ (desired = 4 /\ current["consumer"] < target["consumer"]))
           /\ obligation' = {}
           /\ UNCHANGED <<desired, current, target, affected, step, removed, noopSeen,
                          carried, independentChild>>
IndependentChildChange == /\ observed = 3 /\ desired = 3 /\ step = 2 /\ removed = {}
                          /\ ~independentChild /\ independentChild' = TRUE
                          /\ target' = [target EXCEPT !["consumer"] = 3]
                          /\ UNCHANGED <<desired, observed, current, affected, obligation, step, removed,
                                         noopSeen, carried, badObservation, converged, unrelatedConverged, collectionConverged>>
ThirdChange == /\ independentChild /\ step = 2 /\ step' = 3 /\ desired' = 4
               /\ target' = [target EXCEPT !["q"] = 3]
               /\ affected' = {"q"} /\ obligation' = {"q"}
               /\ UNCHANGED <<observed, current, removed, noopSeen, carried, badObservation,
                              independentChild, converged, unrelatedConverged, collectionConverged>>
Next == FirstChange \/ SecondChange \/ NoopApply \/ (\E id \in Owners : Complete(id))
        \/ CollectConsumer \/ Observe \/ IndependentChildChange \/ ThirdChange
Spec == Init /\ [][Next]_vars
TypeOK == /\ desired \in 1..4 /\ observed \in 1..desired /\ step \in 0..3
          /\ current \in [Owners -> 1..3] /\ target \in [Owners -> 1..3]
          /\ affected \subseteq Owners /\ obligation \subseteq Owners /\ removed \subseteq {"consumer"}
          /\ {noopSeen, carried, badObservation, independentChild, converged,
                unrelatedConverged, collectionConverged} \subseteq BOOLEAN
NoPrematureRevisionObservation == ~badObservation
VersionsNeverOvershoot == \A id \in Owners : current[id] <= target[id]
OverlapSuccessUnreachable == ~(converged /\ noopSeen)
UnrelatedSuccessUnreachable == ~unrelatedConverged
CollectionSuccessUnreachable == ~collectionConverged
=============================================================================
