------------------------------ MODULE PollDispatch ------------------------------
EXTENDS Naturals, Sequences, FiniteSets
CONSTANTS ScopeFilter, HonorLimit, PreserveFIFO, PreserveRest, MarkClaimed,
          RefreshCancellation, RetireUnclaimed
VARIABLES queue, records, claimed, cancelled, terminal, delivered,
          badScope, badLimit, badFIFO, badLoss, badClaim, badSnapshot, badSuppressed,
          mixedPoll, cancelledCleanupDelivered, completedAll
vars == <<queue,records,claimed,cancelled,terminal,delivered,
          badScope,badLimit,badFIFO,badLoss,badClaim,badSnapshot,badSuppressed,
          mixedPoll,cancelledCleanupDelivered,completedAll>>
Op(a) == IF a <= 3 THEN a ELSE a - 3
Host(o) == o
Subtree(h) == IF h = 1 THEN {1,2} ELSE {h}
InitOps == {1,3}
Flags == <<badScope,badLimit,badFIFO,badLoss,badClaim,badSnapshot,badSuppressed,
           mixedPoll,cancelledCleanupDelivered,completedAll>>
Init == /\ queue = <<1,3,2>> /\ records = {1,2,3} /\ claimed = {} /\ cancelled = {}
        /\ terminal = {} /\ delivered = {}
        /\ badScope = FALSE /\ badLimit = FALSE /\ badFIFO = FALSE /\ badLoss = FALSE
        /\ badClaim = FALSE /\ badSnapshot = FALSE /\ badSuppressed = FALSE
        /\ mixedPoll = FALSE /\ cancelledCleanupDelivered = FALSE /\ completedAll = FALSE
Prefix(q,n) == SubSeq(q,1,IF Len(q) < n THEN Len(q) ELSE n)
Ids(q) == {q[i] : i \in 1..Len(q)}
Poll(h, limit) ==
    /\ h \in 1..3 /\ limit \in 1..2
    /\ LET eligible == SelectSeq(queue,LAMBDA a : ~ScopeFilter \/ Host(Op(a)) \in Subtree(h))
           proper == Prefix(SelectSeq(queue,LAMBDA a : Host(Op(a)) \in Subtree(h)),limit)
           n == IF HonorLimit THEN limit ELSE Len(eligible)
           selected == IF PreserveFIFO THEN Prefix(eligible,n)
                       ELSE SubSeq(eligible,IF Len(eligible) < n THEN 1 ELSE Len(eligible)-n+1,Len(eligible))
           ids == Ids(selected)
           normals == ids \cap 1..3
           rest == SelectSeq(queue,LAMBDA a : a \notin ids)
       IN /\ ids # {}
          /\ queue' = (IF PreserveRest THEN rest ELSE <<>>)
          /\ claimed' = (IF MarkClaimed THEN claimed \cup normals ELSE claimed)
          /\ delivered' = delivered \cup ids
          /\ badScope' = (badScope \/ \E a \in ids : Host(Op(a)) \notin Subtree(h))
          /\ badLimit' = (badLimit \/ Len(selected) > limit)
          /\ badFIFO' = (badFIFO \/ selected # proper)
          /\ badLoss' = (badLoss \/ (~PreserveRest /\ rest # <<>>))
          /\ badClaim' = (badClaim \/ ~(normals \subseteq claimed'))
          /\ badSnapshot' = (badSnapshot \/ (~RefreshCancellation /\ normals \cap cancelled # {}))
          /\ badSuppressed' = (badSuppressed \/ normals \cap InitOps \cap cancelled # {})
          /\ mixedPoll' = (mixedPoll \/ (h = 1 /\ 3 \in Ids(queue) /\ 3 \notin ids /\ ids # {}))
          /\ cancelledCleanupDelivered' = (cancelledCleanupDelivered \/ (2 \in normals /\ 2 \in cancelled /\ RefreshCancellation))
    /\ UNCHANGED <<records,cancelled,terminal,completedAll>>
Cancel(o) ==
    /\ o \in (records \ (terminal \cup cancelled))
    /\ LET retire == RetireUnclaimed /\ o \in InitOps /\ o \notin claimed
       IN /\ records' = (IF retire THEN records \ {o} ELSE records)
          /\ queue' = (IF retire THEN SelectSeq(queue,LAMBDA a : Op(a) # o) ELSE Append(queue,o+3))
          /\ cancelled' = (IF retire THEN cancelled ELSE cancelled \cup {o})
    /\ UNCHANGED <<claimed,terminal,delivered,Flags>>
Complete(o) == /\ o \in ((records \cap claimed) \ terminal)
               /\ terminal' = terminal \cup {o}
               /\ completedAll' = (completedAll \/ terminal' = {1,2,3})
               /\ UNCHANGED <<queue,records,claimed,cancelled,delivered,badScope,badLimit,
                              badFIFO,badLoss,badClaim,badSnapshot,badSuppressed,mixedPoll,cancelledCleanupDelivered>>
Ack(o) == /\ o \in terminal /\ records' = records \ {o} /\ claimed' = claimed \ {o}
          /\ cancelled' = cancelled \ {o} /\ terminal' = terminal \ {o}
          /\ queue' = SelectSeq(queue,LAMBDA a : Op(a) # o)
          /\ UNCHANGED <<delivered,Flags>>
Next == (\E h \in 1..3, n \in 1..2 : Poll(h,n))
        \/ (\E o \in 1..3 : Cancel(o) \/ Complete(o) \/ Ack(o))
Spec == Init /\ [][Next]_vars
TypeOK == /\ queue \in Seq(1..6) /\ records \subseteq 1..3 /\ claimed \subseteq records
          /\ cancelled \subseteq records /\ terminal \subseteq records /\ delivered \subseteq 1..6
          /\ {badScope,badLimit,badFIFO,badLoss,badClaim,badSnapshot,badSuppressed,
                mixedPoll,cancelledCleanupDelivered,completedAll} \subseteq BOOLEAN
NoForeignDispatch == ~badScope
BoundedBatch == ~badLimit
ScopedFIFO == ~badFIFO
UnselectedRemain == ~badLoss
DeliveryClaimsOperation == ~badClaim
CurrentCancellationSnapshot == ~badSnapshot
NoSuppressedDispatch == ~badSuppressed
QueueOwnersExist == \A i \in 1..Len(queue) : Op(queue[i]) \in records
QueueIdentityUnique == Len(queue) = Cardinality(Ids(queue))
TerminalWasClaimed == terminal \subseteq claimed
CompletePollSuccessUnreachable == ~(completedAll /\ mixedPoll /\ cancelledCleanupDelivered /\ records = {} /\ queue = <<>>)
ForeignIsolationSuccessUnreachable == ~(3 \in delivered /\ claimed = {3} /\ queue = <<1,2>> /\ records = {1,2,3})
=================================================================================
