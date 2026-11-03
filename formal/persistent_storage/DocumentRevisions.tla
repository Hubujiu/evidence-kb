------------------------- MODULE DocumentRevisions -------------------------
EXTENDS Naturals, TLC
CONSTANTS None, PreserveRevision, CheckExpected, CheckMaximum, PreserveTombstones, CheckTarget
VARIABLES rows, high, pending, completed, snapshot, target, imported, badCAS, badClone,
          badMonotonic, maxRejected, staleRejected, recreated, duplicateImport
Keys == {"a", "b"}
Writers == {"one", "two"}
Max == 3
Absent == [revision |-> 0, live |-> FALSE, value |-> "none"]
Live(r, val) == [revision |-> r, live |-> TRUE, value |-> val]
Tomb(r) == [revision |-> r, live |-> FALSE, value |-> "none"]
Empty == [k \in Keys |-> Absent]
Initial == [a |-> Live(1, "one"), b |-> Tomb(1)]
vars == <<rows, high, pending, completed, snapshot, target, imported, badCAS, badClone,
          badMonotonic, maxRejected, staleRejected, recreated, duplicateImport>>
Init == /\ rows \in {Empty, Initial} /\ high = [k \in Keys |-> rows[k].revision]
        /\ pending = [w \in Writers |-> None] /\ completed = 0 /\ snapshot = None
        /\ target \in {Empty, rows, [a |-> Tomb(2), b |-> Absent]}
        /\ imported = FALSE /\ badCAS = FALSE /\ badClone = FALSE /\ badMonotonic = FALSE
        /\ maxRejected = FALSE /\ staleRejected = FALSE /\ recreated = {} /\ duplicateImport = FALSE
Issue(w, k, operation, expected) ==
    /\ completed < 3 /\ pending[w] = None /\ snapshot = None
    /\ pending' = [pending EXCEPT ![w] = [key |-> k, op |-> operation, expected |-> expected,
                                           observed |-> rows[k].revision, wasLive |-> rows[k].live]]
    /\ UNCHANGED <<rows, high, completed, snapshot, target, imported, badCAS, badClone,
        badMonotonic, maxRejected, staleRejected, recreated, duplicateImport>>
\* BEGIN IMMEDIATE / advisory xact lock serializes actual read-modify-write.
\* Expected revisions are caller observations; Issue need not observe a fresh row.
Execute(w) ==
    /\ pending[w] # None /\ completed < 3 /\ snapshot = None
    /\ LET req == pending[w]
           old == rows[req.key]
           missingDelete == req.op = "delete" /\ ~old.live
           matches == IF req.expected = None THEN TRUE
                      ELSE IF req.op = "put"
                           THEN (req.expected = 0 /\ ~old.live)
                                \/ (req.expected > 0 /\ old.live /\ req.expected = old.revision)
                           ELSE req.expected = old.revision
           allowed == ~missingDelete /\ (matches \/ ~CheckExpected)
                      /\ (old.revision < Max \/ ~CheckMaximum)
           nextRev == IF ~PreserveRevision /\ ~old.live THEN 1
                      ELSE IF old.revision = Max THEN 1 ELSE old.revision + 1
           new == IF req.op = "put" THEN Live(nextRev, w) ELSE Tomb(nextRev)
       IN /\ rows' = IF allowed THEN [rows EXCEPT ![req.key] = new] ELSE rows
          /\ high' = IF allowed /\ nextRev > high[req.key] THEN [high EXCEPT ![req.key] = nextRev] ELSE high
          /\ badCAS' = (badCAS \/ (allowed /\ ~matches))
          /\ badMonotonic' = (badMonotonic \/ (allowed /\ nextRev <= high[req.key]))
          /\ maxRejected' = (maxRejected \/ (~allowed /\ ~missingDelete /\ old.revision = Max))
          /\ staleRejected' = (staleRejected \/ (~allowed /\ old.live /\ req.key \in recreated
                                               /\ req.wasLive /\ req.expected = req.observed
                                               /\ req.expected \in 1..Max
                                               /\ (IF req.expected = None THEN FALSE ELSE req.expected < old.revision)))
          /\ recreated' = IF allowed /\ req.op = "put" /\ ~old.live /\ old.revision > 0
                           THEN recreated \cup {req.key} ELSE recreated
    /\ pending' = [pending EXCEPT ![w] = None] /\ completed' = completed + 1
    /\ UNCHANGED <<snapshot, target, imported, badClone, duplicateImport>>
Export == /\ snapshot = None /\ (\A w \in Writers: pending[w] = None)
          /\ snapshot' = IF PreserveTombstones THEN rows
                         ELSE [k \in Keys |-> IF rows[k].live THEN rows[k] ELSE Absent]
          /\ badClone' = (badClone \/ (snapshot' # rows))
          /\ UNCHANGED <<rows, high, pending, completed, target, imported, badCAS,
              badMonotonic, maxRejected, staleRejected, recreated, duplicateImport>>
Import(identityOK) ==
    /\ snapshot # None
    /\ LET accept == identityOK /\ (target = Empty \/ target = snapshot \/ ~CheckTarget)
       IN /\ target' = IF accept THEN snapshot ELSE target
          /\ badClone' = (badClone \/ (accept /\ target # Empty /\ target # snapshot))
          /\ imported' = (imported \/ accept)
          /\ duplicateImport' = (duplicateImport \/ (imported /\ accept /\ target = snapshot))
    /\ UNCHANGED <<rows, high, pending, completed, snapshot, badCAS, badMonotonic,
        maxRejected, staleRejected, recreated>>
Next == (\E w \in Writers, k \in Keys, op \in {"put", "delete"}, expected \in {None} \cup 0..Max:
            Issue(w, k, op, expected))
        \/ (\E w \in Writers: Execute(w)) \/ Export \/ (\E ok \in BOOLEAN: Import(ok))
Spec == Init /\ [][Next]_vars
Row == [revision: 0..Max, live: BOOLEAN, value: {"none"} \cup Writers]
Request == [key: Keys, op: {"put", "delete"}, expected: {None} \cup 0..Max, observed: 0..Max, wasLive: BOOLEAN]
TypeOK == /\ rows \in [Keys -> Row] /\ target \in [Keys -> Row]
          /\ snapshot \in {None} \cup [Keys -> Row] /\ high \in [Keys -> 0..Max]
          /\ recreated \subseteq Keys
          /\ completed \in 0..3 /\ pending \in [Writers -> {None} \cup Request]
          /\ {imported, badCAS, badClone, badMonotonic, maxRejected, staleRejected,
               duplicateImport} \subseteq BOOLEAN
CompareAndSwap == ~badCAS
MonotoneRevision == ~badMonotonic
ExactClone == ~badClone
TombstonePayload == \A k \in Keys: ~rows[k].live => rows[k].value = "none"
NoRecreateWitness == recreated = {}
NoStaleWitness == ~staleRejected
NoMaximumWitness == ~maxRejected
NoCloneRetryWitness == ~duplicateImport
=============================================================================
