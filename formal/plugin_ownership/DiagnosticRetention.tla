--------------------------- MODULE DiagnosticRetention ---------------------------
EXTENDS Naturals, Sequences, FiniteSets
CONSTANTS GuardIdentity, RejectWithoutEviction, BoundCapacity, DropOldest,
          FilterRuntime, CollectHostRecords
VARIABLES queue, oracle, nextId, childLive, wrongIdentity, rejectedMutation,
          leaked, rejectedAtCap, foreignEvicted, inspectedRoot, inspectedForeign,
          appendedAfterCollect
vars == <<queue, oracle, nextId, childLive, wrongIdentity, rejectedMutation,
          leaked, rejectedAtCap, foreignEvicted, inspectedRoot, inspectedForeign,
          appendedAfterCollect>>
EventHost == [e \in 1..5 |-> CASE e \in {1,4} -> 1 [] e \in {2,5} -> 2 [] OTHER -> 3]
Runtime(h) == IF h = 3 THEN 2 ELSE 1
Keep(q, hosts) == SelectSeq(q, LAMBDA e : EventHost[e] \in hosts)
CorrectAppend(q, e) == Append(IF Len(q) = 2 THEN Tail(q) ELSE q, e)
CandidateAppend(q, e) == Append(IF BoundCapacity /\ Len(q) = 2
                               THEN IF DropOldest THEN Tail(q) ELSE SubSeq(q,1,Len(q)-1)
                               ELSE q, e)
Init == /\ queue = <<>> /\ oracle = <<>> /\ nextId = 1 /\ childLive = TRUE
        /\ wrongIdentity = FALSE /\ rejectedMutation = FALSE /\ leaked = FALSE
        /\ rejectedAtCap = FALSE /\ foreignEvicted = FALSE
        /\ inspectedRoot = FALSE /\ inspectedForeign = FALSE /\ appendedAfterCollect = FALSE
Emit(valid) ==
    /\ valid \in BOOLEAN /\ nextId <= 5 /\ (EventHost[nextId] # 2 \/ childLive)
    /\ LET accept == valid \/ ~GuardIdentity
           candidate == IF accept THEN CandidateAppend(queue,nextId)
                        ELSE IF ~RejectWithoutEviction /\ Len(queue) = 2 THEN Tail(queue) ELSE queue
       IN /\ queue' = candidate
          /\ oracle' = (IF valid THEN CorrectAppend(oracle,nextId) ELSE oracle)
          /\ nextId' = (IF accept THEN nextId + 1 ELSE nextId)
          /\ wrongIdentity' = (wrongIdentity \/ (accept /\ ~valid))
          /\ rejectedMutation' = (rejectedMutation \/ (~valid /\ candidate # queue))
          /\ rejectedAtCap' = (rejectedAtCap \/ (~valid /\ Len(queue) = 2 /\ candidate = queue))
          /\ foreignEvicted' = (foreignEvicted \/ (valid /\ EventHost[nextId] = 3 /\ Len(queue) = 2 /\ EventHost[Head(queue)] = 1))
          /\ appendedAfterCollect' = (appendedAfterCollect \/ (valid /\ ~childLive /\ EventHost[nextId] = 1))
    /\ UNCHANGED <<childLive, leaked, inspectedRoot, inspectedForeign>>
Inspect(r) ==
    /\ r \in {1,2}
    /\ LET result == SelectSeq(queue, LAMBDA e : ~FilterRuntime \/ Runtime(EventHost[e]) = r)
       IN /\ leaked' = (leaked \/ \E i \in 1..Len(result) : Runtime(EventHost[result[i]]) # r)
          /\ inspectedRoot' = (inspectedRoot \/ (r = 1 /\ Len(result) > 0))
          /\ inspectedForeign' = (inspectedForeign \/ (r = 2 /\ Len(result) > 0))
    /\ UNCHANGED <<queue, oracle, nextId, childLive, wrongIdentity, rejectedMutation,
                   rejectedAtCap, foreignEvicted, appendedAfterCollect>>
Collect == /\ childLive /\ childLive' = FALSE
           /\ queue' = (IF CollectHostRecords THEN Keep(queue,{1,3}) ELSE queue)
           /\ oracle' = Keep(oracle,{1,3})
           /\ UNCHANGED <<nextId, wrongIdentity, rejectedMutation, leaked, rejectedAtCap,
                          foreignEvicted, inspectedRoot, inspectedForeign, appendedAfterCollect>>
Next == (\E v \in BOOLEAN : Emit(v)) \/ (\E r \in {1,2} : Inspect(r)) \/ Collect
Spec == Init /\ [][Next]_vars
TypeOK == /\ queue \in Seq(1..5) /\ oracle \in Seq(1..5) /\ nextId \in 1..6
          /\ {childLive,wrongIdentity,rejectedMutation,leaked,rejectedAtCap,
                foreignEvicted,inspectedRoot,inspectedForeign,appendedAfterCollect} \subseteq BOOLEAN
CapacityBounded == Len(queue) <= 2
IdentityValidated == ~wrongIdentity
RejectedLeavesQueue == ~rejectedMutation
CorrectFIFO == queue = oracle
RuntimeInspectionIsolated == ~leaked
CollectedHostAbsent == ~childLive => \A i \in 1..Len(queue) : EventHost[queue[i]] # 2
ImmutableEventOrder == \A i,j \in 1..Len(queue) : i < j => queue[i] < queue[j]
MixedRetentionSuccessUnreachable == ~(foreignEvicted /\ rejectedAtCap /\ inspectedRoot /\ inspectedForeign /\ ~childLive /\ queue = <<3>>)
PostCollectSuccessUnreachable == ~(foreignEvicted /\ rejectedAtCap /\ appendedAfterCollect /\ queue = <<3,4>>)
=================================================================================
