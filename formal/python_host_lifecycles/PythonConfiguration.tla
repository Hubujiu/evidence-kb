------------------------ MODULE PythonConfiguration ------------------------
EXTENDS Naturals
CONSTANTS RevalidateEntry, PublishTogether
VARIABLES revision, pythonRevision, seen, pc, committed
vars == <<revision,pythonRevision,seen,pc,committed>>
Init == /\ revision = 0 /\ pythonRevision = 0 /\ seen = 0 /\ pc = "idle" /\ committed = FALSE
Read == /\ pc = "idle" /\ seen' = revision /\ pc' = "validate"
        /\ UNCHANGED <<revision,pythonRevision,committed>>
ConcurrentUpdate == /\ pc = "validate" /\ revision = 0 /\ revision' = 1
                    /\ pythonRevision' = (IF PublishTogether THEN 1 ELSE 0)
                    /\ UNCHANGED <<seen,pc,committed>>
Validate == /\ pc = "validate" /\ pc' = "check"
            /\ UNCHANGED <<revision,pythonRevision,seen,committed>>
Check == /\ pc = "check"
         /\ pc' = (IF RevalidateEntry /\ seen # revision THEN "idle" ELSE "done")
         /\ committed' = (~RevalidateEntry \/ seen = revision)
         /\ UNCHANGED <<revision,pythonRevision,seen>>
Next == Read \/ ConcurrentUpdate \/ Validate \/ Check
Spec == Init /\ [][Next]_vars
TypeOK == /\ revision \in 0..1 /\ pythonRevision \in 0..1 /\ seen \in 0..1
          /\ pc \in {"idle","validate","check","done"} /\ committed \in BOOLEAN
FreshValidation == committed => seen = revision
NativePythonAgree == revision = pythonRevision
RevalidatedCommitUnreachable == ~(committed /\ revision = 1 /\ seen = 1)
=============================================================================
