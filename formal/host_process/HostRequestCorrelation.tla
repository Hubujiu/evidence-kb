------------------------ MODULE HostRequestCorrelation ------------------------
EXTENDS Naturals, FiniteSets
CONSTANT WrongFuture
VARIABLES allocated, sent, dispatched, replied, failed, receipt, value
Ids == 1..2
vars == <<allocated, sent, dispatched, replied, failed, receipt, value>>
Init == /\ allocated = 0 /\ sent = {} /\ dispatched = {} /\ replied = {}
        /\ failed = FALSE /\ receipt = [i \in Ids |-> "idle"] /\ value = [i \in Ids |-> 0]
Allocate == /\ ~failed /\ allocated < 2 /\ allocated' = allocated + 1
            /\ receipt' = [receipt EXCEPT ![allocated + 1] = "pending"]
            /\ UNCHANGED <<sent, dispatched, replied, failed, value>>
Send(i) == /\ ~failed /\ receipt[i] = "pending" /\ i \notin sent /\ sent' = sent \cup {i}
           /\ UNCHANGED <<allocated, dispatched, replied, failed, receipt, value>>
Dispatch(i) == /\ i \in sent \ dispatched /\ dispatched' = dispatched \cup {i}
               /\ UNCHANGED <<allocated, sent, replied, failed, receipt, value>>
Reply(i) == /\ i \in dispatched \ replied /\ replied' = replied \cup {i}
            /\ UNCHANGED <<allocated, sent, dispatched, failed, receipt, value>>
Receive(i, j) == /\ i \in replied /\ receipt[i] = "pending" /\ receipt[j] = "pending"
                 /\ (j = i \/ WrongFuture)
                 /\ receipt' = [receipt EXCEPT ![j] = "complete"]
                 /\ value' = [value EXCEPT ![j] = i]
                 /\ UNCHANGED <<allocated, sent, dispatched, replied, failed>>
Fail == /\ ~failed /\ failed' = TRUE
        /\ receipt' = [i \in Ids |-> IF receipt[i] = "pending" THEN "unknown" ELSE receipt[i]]
        /\ UNCHANGED <<allocated, sent, dispatched, replied, value>>
Next == Allocate \/ Fail \/ (\E i \in Ids : Send(i) \/ Dispatch(i) \/ Reply(i)
                                     \/ (\E j \in Ids : Receive(i,j)))
Spec == Init /\ [][Next]_vars
TypeOK == /\ allocated \in 0..2 /\ sent \subseteq Ids /\ dispatched \subseteq sent
          /\ replied \subseteq dispatched /\ failed \in BOOLEAN
          /\ receipt \in [Ids -> {"idle", "pending", "complete", "unknown"}]
          /\ value \in [Ids -> 0..2]
ExactCorrelation == \A i \in Ids : receipt[i] = "complete" => value[i] = i
FailureSettlesPending == failed => \A i \in Ids : receipt[i] # "pending"
=============================================================================
