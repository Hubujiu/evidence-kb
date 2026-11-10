---------------------- MODULE EvidenceContinuation ----------------------
EXTENDS Naturals, FiniteSets
CONSTANT ConsumeOtherActor
Tokens == 1..3
Actors == {<<1, 1>>, <<1, 2>>, <<2, 1>>}
VARIABLES pending, owners, issued, expired, consumed, disposed, taken, wrongConsumed, closed
vars == <<pending, owners, issued, expired, consumed, disposed, taken, wrongConsumed, closed>>
Init == /\ pending = {} /\ owners = [t \in Tokens |-> <<1, 1>>] /\ issued = {}
        /\ expired = {} /\ consumed = {} /\ disposed = {} /\ taken = 0
        /\ wrongConsumed = FALSE /\ closed = FALSE
Prepare(t, actor) ==
    /\ ~closed /\ t \notin issued /\ Cardinality(pending \ expired) < 2
    /\ pending' = (pending \ expired) \cup {t}
    /\ disposed' = disposed \cup (pending \cap expired)
    /\ expired' = expired \ pending /\ owners' = [owners EXCEPT ![t] = actor]
    /\ issued' = issued \cup {t}
    /\ UNCHANGED <<consumed, taken, wrongConsumed, closed>>
Age(t) == /\ t \in pending \cup {taken} /\ t \in Tokens /\ t \notin expired
          /\ expired' = expired \cup {t}
          /\ UNCHANGED <<pending, owners, issued, consumed, disposed, taken, wrongConsumed, closed>>
Take(t, actor) == /\ ~closed /\ taken = 0 /\ t \in pending /\ actor = owners[t]
                  /\ pending' = pending \ {t} /\ consumed' = consumed \cup {t} /\ taken' = t
                  /\ UNCHANGED <<owners, issued, expired, disposed, wrongConsumed, closed>>
RejectOther(t, actor) ==
    /\ t \in pending /\ actor # owners[t]
    /\ pending' = IF ConsumeOtherActor THEN pending \ {t} ELSE pending
    /\ wrongConsumed' = (wrongConsumed \/ (t \notin pending'))
    /\ UNCHANGED <<owners, issued, expired, consumed, disposed, taken, closed>>
Finish == /\ taken \in Tokens
          /\ disposed' = disposed \cup {taken} /\ expired' = expired \ {taken} /\ taken' = 0
          /\ UNCHANGED <<pending, owners, issued, consumed, wrongConsumed, closed>>
Close == /\ ~closed /\ taken = 0
         /\ closed' = TRUE /\ disposed' = disposed \cup pending /\ pending' = {} /\ expired' = {}
         /\ UNCHANGED <<owners, issued, consumed, taken, wrongConsumed>>
Next == (\E t \in Tokens, actor \in Actors : Prepare(t, actor) \/ Take(t, actor) \/ RejectOther(t, actor))
        \/ (\E t \in Tokens : Age(t)) \/ Finish \/ Close
Spec == Init /\ [][Next]_vars
TypeOK == /\ pending \subseteq Tokens /\ owners \in [Tokens -> Actors] /\ issued \subseteq Tokens
          /\ expired \subseteq Tokens /\ consumed \subseteq Tokens /\ disposed \subseteq Tokens
          /\ taken \in 0..3 /\ wrongConsumed \in BOOLEAN /\ closed \in BOOLEAN
ActorBoundConsumption == ~wrongConsumed
OneShot == consumed \cap pending = {} /\ disposed \cap pending = {}
PendingCapacity == Cardinality(pending) <= 2
ClaimOwnsContinuation == taken \in Tokens => taken \in consumed /\ taken \notin disposed
ClosedRetainsNothing == closed => pending = {} /\ taken = 0 /\ issued \subseteq disposed
=============================================================================
