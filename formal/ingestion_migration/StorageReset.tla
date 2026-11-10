---------------------------- MODULE StorageReset ----------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS Owners, RequireInitialized, ExclusivePublish, PreserveProtected
VARIABLES pc, initialized, flushed, output, publications, protectedIdentities, originalIdentity
vars == <<pc, initialized, flushed, output, publications, protectedIdentities, originalIdentity>>
Business == {"knowledge", "sessions"}
Protected == {"ledger", "identity"}
InitialProtected == [r \in Protected |-> r]
Active == {"initialize", "write", "flush", "publish"}
Init == /\ pc = [p \in Owners |-> "initialize"]
        /\ initialized = [p \in Owners |-> {}] /\ flushed = [p \in Owners |-> FALSE]
        /\ output = "none" /\ publications = 0
        /\ protectedIdentities = InitialProtected /\ originalIdentity = "old-business"
Initialize(p, role) == /\ pc[p] = "initialize" /\ role \in Business \ initialized[p]
                      /\ initialized' = [initialized EXCEPT ![p] = @ \cup {role}]
                      /\ UNCHANGED <<pc, flushed, output, publications, protectedIdentities, originalIdentity>>
Prepare(p) == /\ pc[p] = "initialize"
              /\ (~RequireInitialized \/ initialized[p] = Business)
              /\ pc' = [pc EXCEPT ![p] = "write"]
              /\ UNCHANGED <<initialized, flushed, output, publications, protectedIdentities, originalIdentity>>
Write(p) == /\ pc[p] = "write" /\ pc' = [pc EXCEPT ![p] = "flush"]
            /\ UNCHANGED <<initialized, flushed, output, publications, protectedIdentities, originalIdentity>>
Flush(p) == /\ pc[p] = "flush" /\ pc' = [pc EXCEPT ![p] = "publish"]
            /\ flushed' = [flushed EXCEPT ![p] = TRUE]
            /\ UNCHANGED <<initialized, output, publications, protectedIdentities, originalIdentity>>
Publish(p) == /\ pc[p] = "publish" /\ (~ExclusivePublish \/ output = "none")
              /\ output' = p /\ publications' = publications + 1
              /\ protectedIdentities' = IF PreserveProtected THEN protectedIdentities ELSE [r \in Protected |-> "reset"]
              /\ pc' = [pc EXCEPT ![p] = "done"]
              /\ UNCHANGED <<initialized, flushed, originalIdentity>>
Lose(p) == /\ pc[p] = "publish" /\ ExclusivePublish /\ output # "none"
           /\ pc' = [pc EXCEPT ![p] = "failed"]
           /\ UNCHANGED <<initialized, flushed, output, publications, protectedIdentities, originalIdentity>>
Crash(p) == /\ pc[p] \in Active /\ pc' = [pc EXCEPT ![p] = "failed"]
            /\ UNCHANGED <<initialized, flushed, output, publications, protectedIdentities, originalIdentity>>
Next == \E p \in Owners : (\E role \in Business : Initialize(p, role))
        \/ Prepare(p) \/ Write(p) \/ Flush(p) \/ Publish(p) \/ Lose(p) \/ Crash(p)
Spec == Init /\ [][Next]_vars
TypeOK == /\ pc \in [Owners -> Active \cup {"done", "failed"}]
          /\ initialized \in [Owners -> SUBSET Business] /\ flushed \in [Owners -> BOOLEAN]
          /\ output \in Owners \cup {"none"} /\ publications \in 0..Cardinality(Owners)
          /\ protectedIdentities \in [Protected -> Protected \cup {"reset"}] /\ originalIdentity = "old-business"
CompleteConfiguration == output # "none" => initialized[output] = Business /\ flushed[output]
NeverOverwrite == publications <= 1
ProtectedIdentities == protectedIdentities = InitialProtected /\ originalIdentity = "old-business"
SuccessUnreachable == output = "none"
=============================================================================
