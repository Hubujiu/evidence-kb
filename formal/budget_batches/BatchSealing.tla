-------------------------- MODULE BatchSealing --------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS SkipDuplicate, DropReviewedCost, SkipSeal
Parents == 1..2
RequestIds == 1..4
Child == 3
OtherChild == 4
Capacity == 2
VARIABLES parentIds, unknown, reviewed, parentCost, seals, phase, drift, rows, known, usage
vars == <<parentIds, unknown, reviewed, parentCost, seals, phase, drift, rows, known, usage>>
Present == {r \in RequestIds : rows[r] # "absent"}
ParentRequests == {parentIds[p] : p \in Parents}
ChildCost == Cardinality({r \in Present : ~known[r] \/ usage[r] = 1})
Init == /\ parentIds \in [Parents -> 1..2] /\ unknown \in SUBSET Parents /\ reviewed = {}
        /\ parentCost = [p \in Parents |-> 1]
        /\ seals \in {[p \in Parents |-> 0], [p \in Parents |-> IF p = 1 THEN 2 ELSE 0]}
        /\ phase = "start" /\ drift = FALSE
        /\ rows = [r \in RequestIds |-> "absent"]
        /\ known = [r \in RequestIds |-> FALSE] /\ usage = [r \in RequestIds |-> 0]
Review(p) == /\ phase = "start" /\ p \in unknown \ reviewed
             /\ reviewed' = reviewed \cup {p}
             /\ parentCost' = [parentCost EXCEPT ![p] = IF DropReviewedCost THEN 0 ELSE @]
             /\ UNCHANGED <<parentIds, unknown, seals, phase, drift, rows, known, usage>>
Competitor == /\ phase = "start" /\ seals[1] \in {0, 2} /\ seals[2] = 0
              /\ seals' = [p \in Parents |-> IF seals[p] = 0 THEN OtherChild ELSE seals[p]]
              /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, phase, drift, rows, known, usage>>
AcquireFirst == /\ phase = "start" /\ phase' = "one-held"
                /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, drift, rows, known, usage>>
AcquireSecond == /\ phase = "one-held" /\ phase' = "both-held"
                 /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, drift, rows, known, usage>>
Validate == /\ phase = "both-held"
            /\ phase' = IF ~drift /\ unknown \subseteq reviewed /\ Cardinality(ParentRequests) = 2
                            /\ seals[1] \in {0, 2, Child} /\ seals[2] \in {0, Child}
                         THEN "written" ELSE "rejected"
            /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, drift, rows, known, usage>>
CommitSecond == /\ phase = "written" /\ phase' = "last-commit"
                /\ seals' = [seals EXCEPT ![2] = Child]
                /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, drift, rows, known, usage>>
CommitFirst == /\ phase = "last-commit" /\ phase' = "ready"
               /\ seals' = [seals EXCEPT ![1] = IF @ = 0 /\ ~SkipSeal THEN Child ELSE @]
               /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, drift, rows, known, usage>>
Abort == /\ phase \in {"one-held", "both-held", "written", "last-commit"} /\ phase' = "aborted"
         /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, drift, rows, known, usage>>
RetrySameChild == /\ phase = "aborted" /\ phase' = "start"
                  /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, drift, rows, known, usage>>
ExternalDrift == /\ ~drift /\ drift' = TRUE
                 /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, phase, rows, known, usage>>
Reserve(r) == /\ phase = "ready" /\ ~drift /\ r \notin Present
              /\ (SkipDuplicate \/ r \notin ParentRequests)
              /\ \A prior \in Present : rows[prior] \notin {"STARTED", "UNKNOWN"}
              /\ ChildCost + 1 <= Capacity
              /\ rows' = [rows EXCEPT ![r] = "STARTED"]
              /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, phase, drift, known, usage>>
Finish(r, status, observed, amount) ==
    /\ rows[r] = "STARTED" /\ rows' = [rows EXCEPT ![r] = status]
    /\ known' = [known EXCEPT ![r] = observed] /\ usage' = [usage EXCEPT ![r] = amount]
    /\ UNCHANGED <<parentIds, unknown, reviewed, parentCost, seals, phase, drift>>
Next == (\E p \in Parents : Review(p)) \/ Competitor \/ AcquireFirst \/ AcquireSecond
        \/ Validate \/ CommitSecond \/ CommitFirst \/ Abort \/ RetrySameChild \/ ExternalDrift
        \/ (\E r \in RequestIds : Reserve(r)
             \/ (\E status \in {"COMPLETE", "ERROR", "UNKNOWN"}, observed \in BOOLEAN, amount \in 0..1 :
                    Finish(r, status, observed, amount)))
Spec == Init /\ [][Next]_vars
TypeOK == /\ parentIds \in [Parents -> 1..2] /\ unknown \subseteq Parents /\ reviewed \subseteq unknown
          /\ parentCost \in [Parents -> 0..1] /\ seals \in [Parents -> 0..4] /\ drift \in BOOLEAN
          /\ phase \in {"start", "one-held", "both-held", "written", "last-commit", "ready", "rejected", "aborted"}
          /\ rows \in [RequestIds -> {"absent", "STARTED", "COMPLETE", "ERROR", "UNKNOWN"}]
          /\ known \in [RequestIds -> BOOLEAN] /\ usage \in [RequestIds -> 0..1]
PersistentFenceBeforeAdmission == phase = "ready" => seals[1] \in {2, Child} /\ seals[2] = Child
DistinctPhysicalIds == Present \cap ParentRequests = {}
ReviewedUnknownRetained == \A p \in unknown : parentCost[p] = 1
SingleUnresolvedChild == Cardinality({r \in Present : rows[r] \in {"STARTED", "UNKNOWN"}}) <= 1
=============================================================================
