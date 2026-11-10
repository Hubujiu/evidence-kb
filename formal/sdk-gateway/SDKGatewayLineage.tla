------------------------- MODULE SDKGatewayLineage -------------------------
EXTENDS Integers, FiniteSets, Sequences, TLC

CONSTANTS Contexts, Payloads, FilenameOrder, UniqueLeafOnly
Attempts == {1, 2}

VARIABLES phase, context, payload, records, order, outcome, parent, selected, decision
vars == <<phase, context, payload, records, order, outcome, parent, selected, decision>>

Leaves == records \ {parent[a]: a \in records}
Complete == {a \in records: outcome[a] = "COMPLETE"}
Covers(r) == (records = {r} /\ parent[r] \notin records)
    \/ (\E other \in records \ {r}:
        records = {r, other} /\ parent[r] = other /\ parent[other] = 0)
ValidLeaf == Cardinality(Leaves) = 1 /\ (\E r \in Leaves: Covers(r))

Init == /\ phase = "READING" /\ context \in Contexts /\ payload \in Payloads
        /\ records = {} /\ order = <<>>
        /\ outcome = [a \in Attempts |-> "NONE"] /\ parent = [a \in Attempts |-> 0]
        /\ selected = 0 /\ decision = "none"

ReadRecord(a, result, prior) == /\ phase = "READING" /\ a \in Attempts \ records
                               /\ result \in {"COMPLETE", "ERROR"} /\ prior \in 0..2
                               /\ records' = records \cup {a} /\ order' = Append(order, a)
                               /\ outcome' = [outcome EXCEPT ![a] = result]
                               /\ parent' = [parent EXCEPT ![a] = prior]
                               /\ UNCHANGED <<phase, context, payload, selected, decision>>

Load == /\ phase = "READING" /\ records # {} /\ phase' = "LOADED"
        /\ IF Complete # {} THEN
              /\ decision' = IF Cardinality(Complete) = 1 THEN "REPLAY" ELSE "REJECT"
              /\ selected' = IF Cardinality(Complete) = 1 THEN CHOOSE a \in Complete: TRUE ELSE 0
           ELSE LET accepted == IF UniqueLeafOnly THEN Cardinality(Leaves) = 1 ELSE ValidLeaf
                IN /\ decision' = IF accepted THEN "RETRY" ELSE "REJECT"
                   /\ selected' = IF ~accepted THEN 0 ELSE
                       IF FilenameOrder THEN order[Len(order)] ELSE CHOOSE a \in Leaves: TRUE
        /\ UNCHANGED <<context, payload, records, order, outcome, parent>>

Next == (\E a \in Attempts, result \in {"COMPLETE", "ERROR"}, prior \in 0..2:
            ReadRecord(a, result, prior)) \/ Load

TypeOK == /\ phase \in {"READING", "LOADED"} /\ context \in Contexts /\ payload \in Payloads
          /\ records \subseteq Attempts /\ Len(order) \in 0..2
          /\ \A i \in 1..Len(order): order[i] \in records
          /\ outcome \in [Attempts -> {"NONE", "COMPLETE", "ERROR"}]
          /\ parent \in [Attempts -> 0..2] /\ selected \in 0..2
          /\ decision \in {"none", "REPLAY", "RETRY", "REJECT"}
RetrySelectsUniqueLineageLeaf == decision = "RETRY" =>
    Complete = {} /\ ValidLeaf /\ selected \in Leaves /\ Covers(selected)
ReplaySelectsUniqueComplete == decision = "REPLAY" =>
    Complete = {selected} /\ outcome[selected] = "COMPLETE"
AmbiguousResultsRejected == phase = "LOADED" /\ Cardinality(Complete) > 1 => decision = "REJECT"
NeverSelectedReverseOrderedLeaf == ~(phase = "LOADED" /\ decision = "RETRY"
    /\ order = <<2, 1>> /\ parent[2] = 1 /\ parent[1] = 0 /\ selected = 2)
NeverRejectedDisconnectedCycle == ~(phase = "LOADED" /\ decision = "REJECT"
    /\ records = Attempts /\ Complete = {} /\ parent[1] = 1 /\ parent[2] = 0)
Spec == Init /\ [][Next]_vars
=============================================================================
