----------------------------- MODULE OCRUpload -----------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS CheckIntegrity, ConsumeFirst, KeepConsumed, ValidateResult, CheckCapacity
Handles == {"a", "b"}
VARIABLES upload, chunks, good, expired, calls, phase, validResult, closed
vars == <<upload, chunks, good, expired, calls, phase, validResult, closed>>
Init == /\ upload = [h \in Handles |-> "new"] /\ chunks = [h \in Handles |-> 0]
        /\ good \in [Handles -> BOOLEAN] /\ expired = {}
        /\ calls = [h \in Handles |-> 0] /\ phase = [h \in Handles |-> "idle"]
        /\ validResult \in [Handles -> BOOLEAN] /\ closed = FALSE
Begin(h) == /\ ~closed /\ upload[h] = "new"
            /\ (CheckCapacity => Cardinality({i \in Handles : upload[i] = "open"}) < 1)
            /\ upload' = [upload EXCEPT ![h] = "open"]
            /\ UNCHANGED <<chunks, good, expired, calls, phase, validResult, closed>>
Append(h) == /\ ~closed /\ upload[h] = "open" /\ h \notin expired /\ chunks[h] < 2
             /\ chunks' = [chunks EXCEPT ![h] = @ + 1]
             /\ UNCHANGED <<upload, good, expired, calls, phase, validResult, closed>>
Age(h) == /\ upload[h] = "open" /\ h \notin expired
          /\ expired' = expired \cup {h}
          /\ UNCHANGED <<upload, chunks, good, calls, phase, validResult, closed>>
Discard(h) == /\ upload[h] = "open"
              /\ upload' = [upload EXCEPT ![h] = "discarded"]
              /\ UNCHANGED <<chunks, good, expired, calls, phase, validResult, closed>>
Consume(h) == /\ ~closed /\ upload[h] = "open" /\ h \notin expired /\ calls[h] < 2
              /\ (CheckIntegrity => chunks[h] = 2 /\ good[h])
              /\ upload' = IF ConsumeFirst THEN [upload EXCEPT ![h] = "consumed"] ELSE upload
              /\ calls' = calls
              /\ phase' = [phase EXCEPT ![h] = "claimed"]
              /\ UNCHANGED <<chunks, good, expired, validResult, closed>>
ProviderStart(h) == /\ phase[h] = "claimed"
                    /\ calls' = [calls EXCEPT ![h] = @ + 1]
                    /\ phase' = [phase EXCEPT ![h] = "provider"]
                    /\ UNCHANGED <<upload, chunks, good, expired, validResult, closed>>
Return(h) == /\ phase[h] = "provider" /\ (ValidateResult => validResult[h])
             /\ phase' = [phase EXCEPT ![h] = "returned"]
             /\ UNCHANGED <<upload, chunks, good, expired, calls, validResult, closed>>
Fail(h) == /\ phase[h] = "provider"
           /\ phase' = [phase EXCEPT ![h] = "failed"]
           /\ upload' = IF KeepConsumed THEN upload ELSE [upload EXCEPT ![h] = "open"]
           /\ UNCHANGED <<chunks, good, expired, calls, validResult, closed>>
Close == /\ ~closed /\ \A h \in Handles : phase[h] \notin {"claimed", "provider"}
         /\ closed' = TRUE
         /\ upload' = [h \in Handles |-> IF upload[h] = "open" THEN "discarded" ELSE upload[h]]
         /\ UNCHANGED <<chunks, good, expired, calls, phase, validResult>>
Next == Close \/ \E h \in Handles : Begin(h) \/ Append(h) \/ Age(h) \/ Discard(h) \/ Consume(h) \/ ProviderStart(h) \/ Return(h) \/ Fail(h)
Spec == Init /\ [][Next]_vars
TypeOK == /\ upload \in [Handles -> {"new", "open", "discarded", "consumed"}]
          /\ chunks \in [Handles -> 0..2] /\ good \in [Handles -> BOOLEAN] /\ expired \subseteq Handles
          /\ calls \in [Handles -> 0..2] /\ phase \in [Handles -> {"idle", "claimed", "provider", "returned", "failed"}]
          /\ validResult \in [Handles -> BOOLEAN] /\ closed \in BOOLEAN
CompleteBeforeProvider == \A h \in Handles : calls[h] > 0 => chunks[h] = 2 /\ good[h]
ConsumedBeforeProvider == \A h \in Handles : calls[h] > 0 => upload[h] = "consumed"
UploadCapacityBound == Cardinality({h \in Handles : upload[h] = "open"}) <= 1
AtMostOnce == \A h \in Handles : calls[h] <= 1
ResultValidated == \A h \in Handles : phase[h] = "returned" => validResult[h]
FailureNeverReopens == \A h \in Handles : phase[h] = "failed" => upload[h] = "consumed"
ClosedOwnsNothing == closed => \A h \in Handles : upload[h] # "open" /\ phase[h] \notin {"claimed", "provider"}
NoReturnedWitness == ~(\E h \in Handles : phase[h] = "returned" /\ calls[h] = 1 /\ validResult[h])
NoFailedWitness == ~(\E h \in Handles : phase[h] = "failed" /\ calls[h] = 1 /\ upload[h] = "consumed")
NoDrainedCloseWitness == ~(closed /\ \E h \in Handles : phase[h] = "returned" /\ calls[h] = 1)
=============================================================================
