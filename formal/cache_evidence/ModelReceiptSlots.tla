----------------------- MODULE ModelReceiptSlots -----------------------
EXTENDS Naturals, FiniteSets
CONSTANTS ExpireInflight, KeepCancelledSlot
Tokens == 1..3
Capacity == 2
VARIABLES pending, active, issued, expired, accepted, confirmed, settled, closed
vars == <<pending, active, issued, expired, accepted, confirmed, settled, closed>>
Present == {t \in Tokens : pending[t] # "absent"}
Init == /\ pending = [t \in Tokens |-> "absent"]
        /\ active = {} /\ issued = {} /\ expired = {}
        /\ accepted = {} /\ confirmed = {} /\ settled = {} /\ closed = FALSE
Begin(t) ==
    LET removed == {k \in Present : k \in expired /\ (ExpireInflight \/ pending[k] = "ready")}
        retained == Present \ removed
    IN /\ ~closed /\ t \notin issued /\ Cardinality(retained) < Capacity
       /\ pending' = [k \in Tokens |-> IF k = t THEN "running"
                       ELSE IF k \in removed THEN "absent" ELSE pending[k]]
       /\ active' = active \cup {t} /\ issued' = issued \cup {t}
       /\ expired' = expired \ removed
       /\ UNCHANGED <<accepted, confirmed, settled, closed>>
Complete(t) == /\ t \in active
               /\ pending' = [pending EXCEPT ![t] = "ready"]
               /\ active' = active \ {t} /\ expired' = expired \ {t}
               /\ UNCHANGED <<issued, accepted, confirmed, settled, closed>>
Fail(t) == /\ t \in active
           /\ pending' = [pending EXCEPT ![t] = "absent"]
           /\ active' = active \ {t} /\ expired' = expired \ {t}
           /\ UNCHANGED <<issued, accepted, confirmed, settled, closed>>
Cancel(t) == /\ t \in active
             /\ pending' = IF KeepCancelledSlot THEN pending ELSE [pending EXCEPT ![t] = "absent"]
             /\ active' = active \ {t} /\ expired' = expired \ {t}
             /\ UNCHANGED <<issued, accepted, confirmed, settled, closed>>
Age(t) == /\ t \in Present /\ t \notin expired
          /\ expired' = expired \cup {t}
          /\ UNCHANGED <<pending, active, issued, accepted, confirmed, settled, closed>>
Settle(t, confirm, cacheSucceeds) ==
    /\ pending[t] = "ready"
    /\ pending' = [pending EXCEPT ![t] = "absent"]
    /\ settled' = settled \cup {t}
    /\ confirmed' = IF confirm /\ t \notin expired THEN confirmed \cup {t} ELSE confirmed
    /\ accepted' = IF confirm /\ cacheSucceeds /\ t \notin expired
                    THEN accepted \cup {t} ELSE accepted
    /\ expired' = expired \ {t}
    /\ UNCHANGED <<active, issued, closed>>
Close == /\ ~closed /\ active = {}
         /\ closed' = TRUE /\ pending' = [t \in Tokens |-> "absent"] /\ expired' = {}
         /\ UNCHANGED <<active, issued, accepted, confirmed, settled>>
Next == (\E t \in Tokens : Begin(t) \/ Complete(t) \/ Fail(t) \/ Cancel(t) \/ Age(t)
            \/ (\E confirm, cacheSucceeds \in BOOLEAN : Settle(t, confirm, cacheSucceeds))) \/ Close
Spec == Init /\ [][Next]_vars
TypeOK == /\ pending \in [Tokens -> {"absent", "running", "ready"}]
          /\ active \subseteq Tokens /\ issued \subseteq Tokens /\ expired \subseteq Tokens
          /\ accepted \subseteq Tokens /\ confirmed \subseteq Tokens /\ settled \subseteq Tokens /\ closed \in BOOLEAN
ReservationRetained == active \subseteq {t \in Tokens : pending[t] = "running"}
NoAbandonedReservation == {t \in Tokens : pending[t] = "running"} \subseteq active
CapacityBound == Cardinality(Present) <= Capacity
ConfirmedReadyOnly == accepted \subseteq confirmed /\ confirmed \subseteq settled /\ settled \subseteq (issued \ active)
ConsumedNeverReappears == settled \cap Present = {}
ClosedOwnsNothing == closed => Present = {} /\ active = {}
=============================================================================
