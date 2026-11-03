------------------------- MODULE KnowledgeTransfer -------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS Owners, AtomicCommit, ResetPublication
VARIABLES owner, pc, requestedIdentity, localRows, localReceipt,
          rows, receipt, publication, successful, repeated, sourceIdentity, targetIdentity
vars == <<owner, pc, requestedIdentity, localRows, localReceipt, rows, receipt,
          publication, successful, repeated, sourceIdentity, targetIdentity>>
Active == {"read", "write", "receipt", "commit"}
Init == /\ owner = "none" /\ pc = [p \in Owners |-> "idle"]
        /\ requestedIdentity = [p \in Owners |-> "none"]
        /\ localRows = [p \in Owners |-> FALSE] /\ localReceipt = [p \in Owners |-> FALSE]
        /\ rows = FALSE /\ receipt = FALSE /\ publication = FALSE
        /\ successful = {} /\ repeated = FALSE
        /\ sourceIdentity = "source-dataset" /\ targetIdentity = "target-dataset"
Acquire(p, identity) == /\ pc[p] = "idle" /\ owner = "none"
                       /\ identity \in {"target-dataset", "wrong-dataset"}
                       /\ owner' = p /\ pc' = [pc EXCEPT ![p] = "read"]
                       /\ requestedIdentity' = [requestedIdentity EXCEPT ![p] = identity]
                       /\ UNCHANGED <<localRows, localReceipt, rows, receipt, publication, successful,
                                      repeated, sourceIdentity, targetIdentity>>
Read(p) == /\ pc[p] = "read"
           /\ LET valid == requestedIdentity[p] = targetIdentity /\ sourceIdentity # targetIdentity
                  reuse == valid /\ receipt /\ rows
                  fresh == valid /\ ~receipt /\ ~rows
              IN /\ pc' = [pc EXCEPT ![p] = IF fresh THEN "write" ELSE "done"]
                 /\ owner' = IF fresh THEN p ELSE "none"
                 /\ repeated' = (repeated \/ reuse)
           /\ UNCHANGED <<requestedIdentity, localRows, localReceipt, rows, receipt, publication,
                          successful, sourceIdentity, targetIdentity>>
Write(p) == /\ pc[p] = "write" /\ localRows' = [localRows EXCEPT ![p] = TRUE]
            /\ rows' = IF AtomicCommit THEN rows ELSE TRUE
            /\ pc' = [pc EXCEPT ![p] = "receipt"]
            /\ UNCHANGED <<owner, requestedIdentity, localReceipt, receipt, publication, successful,
                           repeated, sourceIdentity, targetIdentity>>
Record(p) == /\ pc[p] = "receipt" /\ localReceipt' = [localReceipt EXCEPT ![p] = TRUE]
             /\ pc' = [pc EXCEPT ![p] = "commit"]
             /\ UNCHANGED <<owner, requestedIdentity, localRows, rows, receipt, publication, successful,
                            repeated, sourceIdentity, targetIdentity>>
Commit(p) == /\ pc[p] = "commit" /\ localRows[p] /\ localReceipt[p]
             /\ rows' = TRUE /\ receipt' = TRUE /\ publication' = ~ResetPublication
             /\ successful' = successful \cup {p} /\ pc' = [pc EXCEPT ![p] = "done"] /\ owner' = "none"
             /\ UNCHANGED <<requestedIdentity, localRows, localReceipt, repeated, sourceIdentity, targetIdentity>>
Crash(p) == /\ pc[p] \in Active /\ pc' = [pc EXCEPT ![p] = "crashed"] /\ owner' = "none"
            /\ localRows' = [localRows EXCEPT ![p] = FALSE]
            /\ localReceipt' = [localReceipt EXCEPT ![p] = FALSE]
            /\ UNCHANGED <<requestedIdentity, rows, receipt, publication, successful, repeated,
                           sourceIdentity, targetIdentity>>
Next == \E p \in Owners : (\E identity \in {"target-dataset", "wrong-dataset"} : Acquire(p, identity))
        \/ Read(p) \/ Write(p) \/ Record(p) \/ Commit(p) \/ Crash(p)
Spec == Init /\ [][Next]_vars
TypeOK == /\ owner \in Owners \cup {"none"} /\ pc \in [Owners -> Active \cup {"idle", "done", "crashed"}]
          /\ requestedIdentity \in [Owners -> {"none", "target-dataset", "wrong-dataset"}]
          /\ localRows \in [Owners -> BOOLEAN] /\ localReceipt \in [Owners -> BOOLEAN]
          /\ rows \in BOOLEAN /\ receipt \in BOOLEAN /\ publication \in BOOLEAN
          /\ successful \subseteq Owners /\ repeated \in BOOLEAN
AtomicDataAndReceipt == rows = receipt
PublicationReset == receipt => ~publication
ImmutableDatasetIdentity == /\ sourceIdentity = "source-dataset" /\ targetIdentity = "target-dataset"
                            /\ \A p \in successful : requestedIdentity[p] = targetIdentity
OneFreshImport == Cardinality(successful) <= 1
SuccessUnreachable == ~(receipt /\ repeated)
=============================================================================
