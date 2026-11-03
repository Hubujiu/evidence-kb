------------------------- MODULE PlatformUpload -------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS CheckIntegrity, ReserveFirst, ConsumeFirst, KeepConsumed, CheckCapacity
Handles == {"a", "b"}
VARIABLES upload, chunks, good, expired, calls, phase, reservations, effects,
          unreserved, closed
vars == <<upload, chunks, good, expired, calls, phase, reservations, effects,
          unreserved, closed>>
Init == /\ upload = [h \in Handles |-> "new"]
        /\ chunks = [h \in Handles |-> 0]
        /\ good \in [Handles -> BOOLEAN]
        /\ expired = {} /\ calls = [h \in Handles |-> 0]
        /\ phase = [h \in Handles |-> "idle"] /\ reservations = {}
        /\ effects = {} /\ unreserved = {} /\ closed = FALSE
Begin(h) == /\ ~closed /\ upload[h] = "new"
            /\ (CheckCapacity => Cardinality({i \in Handles : upload[i] = "open"}) < 1)
            /\ upload' = [upload EXCEPT ![h] = "open"]
            /\ UNCHANGED <<chunks, good, expired, calls, phase, reservations, effects, unreserved, closed>>
Append(h) == /\ ~closed /\ upload[h] = "open" /\ h \notin expired /\ chunks[h] < 2
             /\ chunks' = [chunks EXCEPT ![h] = @ + 1]
             /\ UNCHANGED <<upload, good, expired, calls, phase, reservations, effects, unreserved, closed>>
Age(h) == /\ upload[h] = "open" /\ h \notin expired
          /\ expired' = expired \cup {h}
          /\ UNCHANGED <<upload, chunks, good, calls, phase, reservations, effects, unreserved, closed>>
Discard(h) == /\ upload[h] = "open"
              /\ upload' = [upload EXCEPT ![h] = "discarded"]
              /\ UNCHANGED <<chunks, good, expired, calls, phase, reservations, effects, unreserved, closed>>
Finish(h) == /\ ~closed /\ upload[h] = "open" /\ h \notin expired
             /\ (CheckIntegrity => chunks[h] = 2 /\ good[h])
             /\ (ReserveFirst => Cardinality(reservations) < 1)
             /\ calls[h] < 2
             /\ reservations' = IF Cardinality(reservations) < 1 THEN reservations \cup {h} ELSE reservations
             /\ unreserved' = IF Cardinality(reservations) < 1 THEN unreserved ELSE unreserved \cup {h}
             /\ upload' = IF ConsumeFirst THEN [upload EXCEPT ![h] = "consumed"] ELSE upload
             /\ calls' = calls
             /\ phase' = [phase EXCEPT ![h] = "claimed"]
             /\ UNCHANGED <<chunks, good, expired, effects, closed>>
Dispatch(h) == /\ phase[h] = "claimed"
               /\ calls' = [calls EXCEPT ![h] = @ + 1]
               /\ phase' = [phase EXCEPT ![h] = "running"]
               /\ UNCHANGED <<upload, chunks, good, expired, reservations, effects, unreserved, closed>>
ProviderDone(h) == /\ phase[h] = "running"
                  /\ effects' = effects \cup {h}
                  /\ phase' = [phase EXCEPT ![h] = "result"]
                  /\ UNCHANGED <<upload, chunks, good, expired, calls, reservations, unreserved, closed>>
PublishInline(h) == /\ phase[h] = "result" /\ h \in reservations
              /\ phase' = [phase EXCEPT ![h] = "published"]
              /\ reservations' = reservations \ {h}
              /\ UNCHANGED <<upload, chunks, good, expired, calls, effects, unreserved, closed>>
PublishTransfer(h) == /\ phase[h] = "result" /\ h \in reservations
                      /\ phase' = [phase EXCEPT ![h] = "published"]
                      /\ UNCHANGED <<upload, chunks, good, expired, calls, reservations, effects, unreserved, closed>>
ReleaseResult(h) == /\ phase[h] = "published" /\ h \in reservations
                    /\ reservations' = reservations \ {h}
                    /\ UNCHANGED <<upload, chunks, good, expired, calls, phase, effects, unreserved, closed>>
Fail(h) == /\ phase[h] \in {"running", "result"}
           /\ phase' = [phase EXCEPT ![h] = "failed"]
           /\ reservations' = reservations \ {h}
           /\ upload' = IF KeepConsumed THEN upload ELSE [upload EXCEPT ![h] = "open"]
           /\ UNCHANGED <<chunks, good, expired, calls, effects, unreserved, closed>>
Close == /\ ~closed /\ \A h \in Handles : phase[h] \notin {"claimed", "running", "result"}
         /\ closed' = TRUE /\ reservations' = {}
         /\ upload' = [h \in Handles |-> IF upload[h] = "open" THEN "discarded" ELSE upload[h]]
         /\ UNCHANGED <<chunks, good, expired, calls, phase, effects, unreserved>>
Next == Close \/ \E h \in Handles : Begin(h) \/ Append(h) \/ Age(h) \/ Discard(h) \/ Finish(h)
                                  \/ Dispatch(h) \/ ProviderDone(h) \/ PublishInline(h) \/ PublishTransfer(h) \/ ReleaseResult(h) \/ Fail(h)
Spec == Init /\ [][Next]_vars
TypeOK == /\ upload \in [Handles -> {"new", "open", "discarded", "consumed"}]
          /\ chunks \in [Handles -> 0..2] /\ good \in [Handles -> BOOLEAN]
          /\ expired \subseteq Handles /\ calls \in [Handles -> 0..2]
          /\ phase \in [Handles -> {"idle", "claimed", "running", "result", "published", "failed"}]
          /\ reservations \subseteq Handles /\ effects \subseteq Handles
          /\ unreserved \subseteq Handles /\ closed \in BOOLEAN
CompleteBeforeDispatch == \A h \in Handles : calls[h] > 0 => chunks[h] = 2 /\ good[h]
ReserveBeforeDispatch == unreserved = {}
ConsumedBeforeDispatch == \A h \in Handles : calls[h] > 0 => upload[h] = "consumed"
AtMostOnce == \A h \in Handles : calls[h] <= 1
UploadCapacityBound == Cardinality({h \in Handles : upload[h] = "open"}) <= 1
CapacityBound == Cardinality(reservations) <= 1
FailedRetainsConsumption == \A h \in Handles : phase[h] = "failed" => upload[h] = "consumed"
ClosedOwnsNothing == closed => reservations = {} /\ \A h \in Handles : upload[h] # "open"
NoPublishedWitness == ~(\E h \in Handles : phase[h] = "published" /\ h \in effects /\ calls[h] = 1)
NoCommittedFailureWitness == ~(\E h \in Handles : phase[h] = "failed" /\ h \in effects /\ upload[h] = "consumed")
NoTransferHeldWitness == ~(\E h \in Handles : phase[h] = "published" /\ h \in reservations /\ h \in effects)
=============================================================================
