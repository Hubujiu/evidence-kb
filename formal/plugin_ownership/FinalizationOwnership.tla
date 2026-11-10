---------------------- MODULE FinalizationOwnership ----------------------
EXTENDS Naturals, FiniteSets, Sequences
CONSTANTS ChooseLIFO, KeepFailedResource, AckOnlyTerminal, AckOnlyOwner,
          KeepCompletion, RetainOperationOwners, SortListeners
VARIABLES phase, resources, done, pendingResource, pendingOperation, queued,
          claimed, outstanding, terminal, outcome, nextID, failedOnce, retried,
          badOrder, releasedOnError, ackCount, earlyAck, foreignAck, retired,
          collected, listenerSeen, badListeners, duplicateSeen, conflictSeen,
          overwritten, rejectedAcks
vars == <<phase, resources, done, pendingResource, pendingOperation, queued,
          claimed, outstanding, terminal, outcome, nextID, failedOnce, retried,
          badOrder, releasedOnError, ackCount, earlyAck, foreignAck, retired,
          collected, listenerSeen, badListeners, duplicateSeen, conflictSeen,
          overwritten, rejectedAcks>>
Ops == 0..7
ChildOps == 1..7
Priority(r) == IF r = 2 THEN 0 ELSE 1
ScopeDepth(r) == IF r = 1 THEN 0 ELSE 1
Before(a,b) == Priority(a) < Priority(b)
               \/ (Priority(a) = Priority(b) /\ ScopeDepth(a) < ScopeDepth(b))
               \/ (Priority(a) = Priority(b) /\ ScopeDepth(a) = ScopeDepth(b) /\ a < b)
RECURSIVE Ordered(_, _)
Ordered(items, usePriority) ==
    IF items = {} THEN <<>>
    ELSE LET first == CHOOSE r \in items : \A other \in items \ {r} :
                        IF usePriority THEN Before(r, other) ELSE r < other
         IN <<first>> \o Ordered(items \ {first}, usePriority)
Init == /\ phase = "active" /\ resources = {2} /\ done = {}
        /\ pendingResource = 0 /\ pendingOperation = 0 /\ queued = 0
        /\ claimed = {0,1,2} /\ outstanding = {0,1,2} /\ terminal = {0,1}
        /\ outcome = [op \in Ops |-> IF op \in {0,1} THEN "ok" ELSE "pending"]
        /\ nextID = 3 /\ failedOnce = FALSE /\ retried = FALSE
        /\ badOrder = FALSE /\ releasedOnError = FALSE /\ ackCount = 0
        /\ earlyAck = FALSE /\ foreignAck = FALSE /\ retired = FALSE /\ collected = FALSE
        /\ listenerSeen = FALSE /\ badListeners = FALSE /\ duplicateSeen = FALSE
        /\ conflictSeen = FALSE /\ overwritten = FALSE /\ rejectedAcks = {}
Register(r) == /\ r \in 3..4 \ resources /\ 2 \notin terminal /\ r - 1 \in resources
               /\ resources' = resources \cup {r}
               /\ UNCHANGED <<phase, done, pendingResource, pendingOperation, queued, claimed,
                              outstanding, terminal, outcome, nextID, failedOnce, retried, badOrder,
                              releasedOnError, ackCount, earlyAck, foreignAck, retired, collected,
                              listenerSeen, badListeners, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
Remove == /\ phase = "active" /\ phase' = "draining"
          /\ UNCHANGED <<resources, done, pendingResource, pendingOperation, queued, claimed,
                         outstanding, terminal, outcome, nextID, failedOnce, retried, badOrder,
                         releasedOnError, ackCount, earlyAck, foreignAck, retired, collected,
                         listenerSeen, badListeners, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
CompleteCall == /\ 2 \notin terminal /\ terminal' = terminal \cup {2}
                /\ outcome' = [outcome EXCEPT ![2] = "ok"]
                /\ UNCHANGED <<phase, resources, done, pendingResource, pendingOperation, queued, claimed,
                               outstanding, nextID, failedOnce, retried, badOrder, releasedOnError,
                               ackCount, earlyAck, foreignAck, retired, collected, listenerSeen,
                               badListeners, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
Schedule ==
    /\ phase \in {"draining", "cleaning"} /\ 2 \in terminal /\ pendingOperation = 0 /\ nextID <= 7
    /\ pendingResource' = (IF resources \ done = {} THEN 0
                            ELSE CHOOSE r \in resources \ done : \A other \in resources \ done :
                                   IF ChooseLIFO THEN r >= other ELSE r <= other)
    /\ pendingOperation' = nextID /\ queued' = nextID /\ nextID' = nextID + 1
    /\ outstanding' = outstanding \cup {nextID} /\ phase' = "cleaning"
    /\ UNCHANGED <<resources, done, claimed, terminal, outcome, failedOnce, retried, badOrder,
                   releasedOnError, ackCount, earlyAck, foreignAck, retired, collected,
                   listenerSeen, badListeners, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
ClaimCleanup == /\ queued # 0 /\ claimed' = claimed \cup {queued} /\ queued' = 0
                /\ UNCHANGED <<phase, resources, done, pendingResource, pendingOperation, outstanding,
                               terminal, outcome, nextID, failedOnce, retried, badOrder, releasedOnError,
                               ackCount, earlyAck, foreignAck, retired, collected, listenerSeen,
                               badListeners, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
FinishCleanup(ok) ==
    /\ pendingOperation # 0 /\ pendingOperation \in claimed /\ ok \in BOOLEAN
    /\ ok \/ ~failedOnce
    /\ terminal' = terminal \cup {pendingOperation}
    /\ outcome' = [outcome EXCEPT ![pendingOperation] = IF ok THEN "ok" ELSE "error"]
    /\ done' = (IF pendingResource # 0 /\ (ok \/ ~KeepFailedResource)
                THEN done \cup {pendingResource} ELSE done)
    /\ badOrder' = (badOrder \/ (ok /\ pendingResource # 0
                                /\ (\E r \in resources \ done : r > pendingResource)))
    /\ releasedOnError' = (releasedOnError \/ (~ok /\ ~KeepFailedResource /\ pendingResource # 0))
    /\ phase' = (IF ~ok THEN "cleanup_failed" ELSE IF pendingResource = 0 THEN "disposed" ELSE "cleaning")
    /\ failedOnce' = (failedOnce \/ ~ok) /\ pendingOperation' = 0 /\ pendingResource' = 0
    /\ UNCHANGED <<resources, queued, claimed, outstanding, nextID, retried, ackCount, earlyAck,
                   foreignAck, retired, collected, listenerSeen, badListeners, duplicateSeen,
                   conflictSeen, overwritten, rejectedAcks>>
Retry == /\ phase = "cleanup_failed" /\ phase' = "cleaning" /\ retried' = TRUE
         /\ UNCHANGED <<resources, done, pendingResource, pendingOperation, queued, claimed,
                        outstanding, terminal, outcome, nextID, failedOnce, badOrder, releasedOnError,
                        ackCount, earlyAck, foreignAck, retired, collected, listenerSeen, badListeners,
                        duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
Ack(batch) ==
    /\ ackCount < 2
    /\ batch \in {{1}, {1,2}, {0,1}, outstanding \cap ChildOps, {pendingOperation}}
    /\ LET selected == batch \cap outstanding
           valid == (~AckOnlyTerminal \/ selected \subseteq terminal)
                    /\ (~AckOnlyOwner \/ 0 \notin selected)
       IN /\ outstanding' = (IF valid THEN outstanding \ selected ELSE outstanding)
          /\ earlyAck' = (earlyAck \/ (valid /\ ~(selected \subseteq terminal)))
          /\ foreignAck' = (foreignAck \/ (valid /\ 0 \in selected))
          /\ rejectedAcks' = (IF valid THEN rejectedAcks
                               ELSE rejectedAcks \cup (IF selected \subseteq terminal THEN {} ELSE {"pending"})
                                                 \cup (IF 0 \in selected THEN {"foreign"} ELSE {}))
    /\ ackCount' = ackCount + 1
    /\ UNCHANGED <<phase, resources, done, pendingResource, pendingOperation, queued, claimed,
                   terminal, outcome, nextID, failedOnce, retried, badOrder, releasedOnError,
                   retired, collected, listenerSeen, badListeners, duplicateSeen, conflictSeen, overwritten>>
CompleteAgain(op, different) ==
    /\ op \in terminal \cap outstanding \cap ChildOps /\ different \in BOOLEAN
    /\ (IF different THEN ~conflictSeen ELSE ~duplicateSeen)
    /\ conflictSeen' = (conflictSeen \/ different) /\ duplicateSeen' = (duplicateSeen \/ ~different)
    /\ outcome' = (IF different /\ ~KeepCompletion
                   THEN [outcome EXCEPT ![op] = IF @ = "ok" THEN "error" ELSE "ok"] ELSE outcome)
    /\ overwritten' = (overwritten \/ (different /\ ~KeepCompletion))
    /\ UNCHANGED <<phase, resources, done, pendingResource, pendingOperation, queued, claimed,
                   outstanding, terminal, nextID, failedOnce, retried, badOrder, releasedOnError,
                   ackCount, earlyAck, foreignAck, retired, collected, listenerSeen, badListeners, rejectedAcks>>
ReadListeners ==
    /\ LET visible == {1} \cup (IF phase = "active" THEN resources \ done ELSE {})
           result == Ordered(visible, SortListeners)
       IN /\ badListeners' = (badListeners \/ result # Ordered(visible, TRUE))
          /\ listenerSeen' = (listenerSeen \/ result = <<2,1,3,4>>)
    /\ UNCHANGED <<phase, resources, done, pendingResource, pendingOperation, queued, claimed,
                   outstanding, terminal, outcome, nextID, failedOnce, retried, badOrder, releasedOnError,
                   ackCount, earlyAck, foreignAck, retired, collected, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
Retire == /\ phase = "disposed" /\ ~retired /\ retired' = TRUE
          /\ UNCHANGED <<phase, resources, done, pendingResource, pendingOperation, queued, claimed,
                         outstanding, terminal, outcome, nextID, failedOnce, retried, badOrder,
                         releasedOnError, ackCount, earlyAck, foreignAck, collected, listenerSeen,
                         badListeners, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
Prune == /\ retired /\ ~collected /\ (~RetainOperationOwners \/ outstanding \cap ChildOps = {})
         /\ collected' = TRUE
         /\ UNCHANGED <<phase, resources, done, pendingResource, pendingOperation, queued, claimed,
                        outstanding, terminal, outcome, nextID, failedOnce, retried, badOrder,
                        releasedOnError, ackCount, earlyAck, foreignAck, retired, listenerSeen,
                        badListeners, duplicateSeen, conflictSeen, overwritten, rejectedAcks>>
Next == (\E r \in 3..4 : Register(r)) \/ Remove \/ CompleteCall \/ Schedule \/ ClaimCleanup
        \/ (\E ok \in BOOLEAN : FinishCleanup(ok)) \/ Retry
        \/ (\E batch \in SUBSET Ops : Ack(batch))
        \/ (\E op \in Ops, different \in BOOLEAN : CompleteAgain(op, different))
        \/ ReadListeners \/ Retire \/ Prune
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"active", "draining", "cleaning", "cleanup_failed", "disposed"}
          /\ done \subseteq resources /\ resources \subseteq 2..4
          /\ pendingResource \in {0} \cup resources /\ pendingOperation \in Ops /\ queued \in Ops
          /\ claimed \subseteq Ops /\ outstanding \subseteq Ops /\ terminal \subseteq claimed
          /\ outcome \in [Ops -> {"pending", "ok", "error"}] /\ nextID \in 3..8 /\ ackCount \in 0..2
          /\ {failedOnce, retried, badOrder, releasedOnError, earlyAck, foreignAck, retired, collected,
                listenerSeen, badListeners, duplicateSeen, conflictSeen, overwritten} \subseteq BOOLEAN
          /\ rejectedAcks \subseteq {"pending", "foreign"}
ReverseResourceCompletion == ~badOrder
FailureRetainsResource == ~releasedOnError
NoPendingAcknowledgement == ~earlyAck
NoForeignAcknowledgement == ~foreignAck
CompletionImmutable == ~overwritten
NoPrematureRetirement == collected => outstanding \cap ChildOps = {}
ListenerOrder == ~badListeners
DisposedOwnsNoResource == phase = "disposed" => done = resources
FinalizationSuccessUnreachable == ~(collected /\ retried /\ resources = 2..4 /\ listenerSeen /\ duplicateSeen /\ conflictSeen)
FailureRetentionUnreachable == ~(phase = "cleanup_failed" /\ failedOnce /\ done = {} /\ pendingOperation = 0)
AckRejectionsUnreachable == rejectedAcks # {"pending", "foreign"}
=============================================================================
