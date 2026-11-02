----------------------------- MODULE OCRRecovery -----------------------------
EXTENDS Naturals, FiniteSets, Sequences
CONSTANTS SkipCompleted, TrackLocalCompletion
VARIABLES round, pc, completed, counts, pending, output, allowed, localCompleted, repeatedCall
vars == <<round, pc, completed, counts, pending, output, allowed, localCompleted, repeatedCall>>
Pages == 1..2
Init == /\ round = 1 /\ pc = 1 /\ localCompleted \in SUBSET Pages
        /\ completed = IF TrackLocalCompletion THEN localCompleted ELSE {}
        /\ counts = [p \in Pages |-> IF p \in localCompleted THEN 1 ELSE 0]
        /\ pending = Pages \ completed
        /\ output = <<>> /\ allowed \in BOOLEAN /\ repeatedCall = FALSE
Call(success) == /\ pc \in Pages /\ allowed /\ pc \in pending /\ success \in BOOLEAN
                 /\ counts' = [counts EXCEPT ![pc] = @ + IF success THEN 1 ELSE 0]
                 /\ completed' = IF success THEN completed \cup {pc} ELSE completed
                 /\ pc' = pc + 1
                 /\ repeatedCall' = (repeatedCall \/ pc \in completed \cup localCompleted)
                 /\ UNCHANGED <<round, pending, output, allowed, localCompleted>>
Skip == /\ pc \in Pages /\ (~allowed \/ pc \notin pending) /\ pc' = pc + 1
        /\ UNCHANGED <<round, completed, counts, pending, output, allowed, localCompleted, repeatedCall>>
Assemble == /\ pc = 3 /\ pc' = 4
            /\ output' = SelectSeq(<<1, 2>>, LAMBDA p: p \in completed)
            /\ UNCHANGED <<round, completed, counts, pending, allowed, localCompleted, repeatedCall>>
ExplicitResume == /\ pc = 4 /\ round = 1 /\ round' = 2 /\ pc' = 1
                  /\ pending' = IF SkipCompleted THEN Pages \ completed ELSE Pages
                  /\ UNCHANGED <<completed, counts, output, allowed, localCompleted, repeatedCall>>
Next == (\E success \in BOOLEAN : Call(success)) \/ Skip \/ Assemble \/ ExplicitResume
Spec == Init /\ [][Next]_vars
TypeOK == /\ round \in 1..2 /\ pc \in 1..4 /\ completed \subseteq Pages
          /\ counts \in [Pages -> 0..2] /\ pending \subseteq Pages
          /\ output \in {<<>>, <<1>>, <<2>>, <<1, 2>>} /\ allowed \in BOOLEAN
          /\ localCompleted \subseteq Pages /\ repeatedCall \in BOOLEAN
NoCompletedPageCall == ~repeatedCall
NoDuplicateSuccess == \A p \in Pages : counts[p] <= 1
CanonicalOrder == pc = 4 => output = SelectSeq(<<1, 2>>, LAMBDA p: p \in completed)
PolicyBound == ~allowed => completed = localCompleted
LocalCompletionRetained == localCompleted \subseteq completed
RecoveryUnreachable == ~(round = 2 /\ pc = 4 /\ localCompleted # Pages /\ completed = Pages)
=============================================================================
