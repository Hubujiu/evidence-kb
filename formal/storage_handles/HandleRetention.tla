-------------------------- MODULE HandleRetention --------------------------
EXTENDS Naturals, FiniteSets, Sequences
CONSTANTS EnforceCapacity, EvictOldest, ReplayCachedFinish
VARIABLES active, used, cache, outcomes, replayed, observed, observedToken
Tokens == 1..3
Capacity == 2
Retention == 2
Statuses == {"COMMITTED", "ROLLED_BACK", "UNKNOWN"}
vars == <<active, used, cache, outcomes, replayed, observed, observedToken>>
Init == /\ active = {} /\ used = {} /\ cache = <<>>
        /\ outcomes = [t \in Tokens |-> "none"] /\ replayed = FALSE
        /\ observed = "none" /\ observedToken = 0
Open(t) == /\ t \notin used /\ (Cardinality(active) < Capacity \/ ~EnforceCapacity)
           /\ active' = active \cup {t} /\ used' = used \cup {t}
           /\ UNCHANGED <<cache, outcomes, replayed, observed, observedToken>>
Finish(t) == /\ t \in active /\ active' = active \ {t}
             /\ outcomes' \in [Tokens -> Statuses \cup {"none"}]
             /\ outcomes'[t] \in Statuses
             /\ \A other \in Tokens \ {t}: outcomes'[other] = outcomes[other]
             /\ LET appended == Append(cache, t)
                IN cache' = IF EvictOldest /\ Len(appended) > Retention THEN Tail(appended) ELSE appended
             /\ UNCHANGED <<used, replayed, observed, observedToken>>
Observe(t) == /\ t \in used \ active
              /\ observedToken' = t
              /\ observed' = IF t \in {cache[i]: i \in 1..Len(cache)} THEN outcomes[t] ELSE "CLOSED"
              /\ replayed' = ReplayCachedFinish
              /\ UNCHANGED <<active, used, cache, outcomes>>
Next == \E t \in Tokens: Open(t) \/ Finish(t) \/ Observe(t)
Spec == Init /\ [][Next]_vars
TypeOK == /\ active \subseteq used /\ used \subseteq Tokens /\ cache \in Seq(Tokens)
          /\ outcomes \in [Tokens -> Statuses \cup {"none"}]
          /\ replayed \in BOOLEAN /\ observed \in Statuses \cup {"none", "CLOSED"}
          /\ observedToken \in 0..3
BoundedActive == Cardinality(active) <= Capacity
BoundedReceipts == Len(cache) <= Retention
NoReplay == ~replayed
OnlyFinishedCached == \A i \in 1..Len(cache): cache[i] \notin active /\ outcomes[cache[i]] \in Statuses
NoFabricatedReceipt == observed \in Statuses => observed = outcomes[observedToken]
NoFreshOpen == active = {}
NoEvictedObservation == observed # "CLOSED"
=============================================================================
