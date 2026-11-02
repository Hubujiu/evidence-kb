---------------------------- MODULE PageFallback ----------------------------
EXTENDS Naturals, FiniteSets
CONSTANT IsolateFailure
VARIABLES pc, allowed, outcomes, attempted, primaryFailed
vars == <<pc, allowed, outcomes, attempted, primaryFailed>>
Pages == 1..2
Init == /\ pc = 0 /\ allowed \in BOOLEAN
        /\ outcomes = [p \in Pages |-> "pending"] /\ attempted = {}
        /\ primaryFailed = {}
Primary == /\ pc = 0 /\ primaryFailed' = Pages /\ pc' = 1
           /\ UNCHANGED <<allowed, outcomes, attempted>>
Blocked == /\ pc = 1 /\ ~allowed /\ pc' = 3
           /\ outcomes' = [p \in Pages |-> "blocked"]
           /\ UNCHANGED <<allowed, attempted, primaryFailed>>
Fallback(result) == /\ pc \in Pages /\ allowed /\ result \in {"failed", "success"}
                    /\ attempted' = attempted \cup {pc}
                    /\ IF result = "failed" /\ ~IsolateFailure
                       THEN /\ outcomes' = [p \in Pages |-> IF p >= pc THEN "failed" ELSE outcomes[p]]
                            /\ pc' = 3
                       ELSE /\ outcomes' = [outcomes EXCEPT ![pc] = result]
                            /\ pc' = pc + 1
                    /\ UNCHANGED <<allowed, primaryFailed>>
Next == Primary \/ Blocked \/ (\E result \in {"failed", "success"} : Fallback(result))
Spec == Init /\ [][Next]_vars
TypeOK == /\ pc \in 0..3 /\ allowed \in BOOLEAN /\ attempted \subseteq Pages
          /\ primaryFailed \subseteq Pages
          /\ outcomes \in [Pages -> {"pending", "blocked", "failed", "success"}]
IndependentPages == pc = 3 /\ allowed => attempted = Pages
PrimaryHistoryRetained == pc > 0 => primaryFailed = Pages
PolicyBound == ~allowed => attempted = {}
PartialRecoveryUnreachable == ~(pc = 3 /\ outcomes[1] = "failed" /\ outcomes[2] = "success")
=============================================================================
