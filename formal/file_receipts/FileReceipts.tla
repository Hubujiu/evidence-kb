---------------------------- MODULE FileReceipts ----------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Safe, MaxAttempts, Actors
VARIABLES pending, terminal, status, admitted, settled, nextId, owner,
          pc, selected, operation, outcome
vars == <<pending, terminal, status, admitted, settled, nextId, owner,
          pc, selected, operation, outcome>>
Ids == 1..MaxAttempts
Nonterminal == {"STARTED", "UNKNOWN"}
Terminal == {"COMPLETE", "ERROR"}
Init == /\ pending = 0 /\ terminal = 0
        /\ status = [i \in Ids |-> "ABSENT"]
        /\ settled = [i \in Ids |-> "ABSENT"]
        /\ admitted = {} /\ nextId = 1 /\ owner = 0
        /\ pc = [a \in Actors |-> "Idle"]
        /\ selected = [a \in Actors |-> 0]
        /\ operation = [a \in Actors |-> "Begin"]
        /\ outcome = [a \in Actors |-> "ERROR"]
CanStart(a) == pc[a] = "Idle" /\ (~Safe \/ owner = 0)
StartBegin(a) == /\ CanStart(a) /\ nextId <= MaxAttempts
                 /\ owner' = IF Safe THEN a ELSE owner
                 /\ pc' = [pc EXCEPT ![a] = "BeginCheck"]
                 /\ selected' = [selected EXCEPT ![a] = nextId]
                 /\ nextId' = nextId + 1
                 /\ operation' = [operation EXCEPT ![a] = "Begin"]
                 /\ UNCHANGED <<pending, terminal, status, admitted, settled, outcome>>
BeginClaim(a) == /\ pc[a] = "BeginCheck" /\ pending = 0
                 /\ pending' = selected[a]
                 /\ pc' = [pc EXCEPT ![a] = "BeginPersist"]
                 /\ UNCHANGED <<terminal, status, admitted, settled, nextId, owner,
                                  selected, operation, outcome>>
BeginPersist(a) == /\ pc[a] = "BeginPersist"
                   /\ status' = [status EXCEPT ![selected[a]] = "STARTED"]
                   /\ pc' = [pc EXCEPT ![a] = "BeginReturn"]
                   /\ UNCHANGED <<pending, terminal, admitted, settled, nextId, owner,
                                    selected, operation, outcome>>
BeginReturn(a) == /\ pc[a] = "BeginReturn"
                  /\ admitted' = admitted \cup {selected[a]}
                  /\ owner' = IF Safe THEN 0 ELSE owner
                  /\ pc' = [pc EXCEPT ![a] = "Idle"]
                  /\ UNCHANGED <<pending, terminal, status, settled, nextId,
                                   selected, operation, outcome>>
StartTransition(a, i, op, result) ==
    /\ CanStart(a) /\ i \in admitted
    /\ op \in {"Finish", "Reconcile"}
    /\ result \in (IF op = "Finish" THEN Terminal \cup {"UNKNOWN"} ELSE Terminal)
    /\ owner' = IF Safe THEN a ELSE owner
    /\ selected' = [selected EXCEPT ![a] = i]
    /\ operation' = [operation EXCEPT ![a] = op]
    /\ outcome' = [outcome EXCEPT ![a] = result]
    /\ pc' = [pc EXCEPT ![a] = "Read"]
    /\ UNCHANGED <<pending, terminal, status, admitted, settled, nextId>>
Valid(a) == /\ status[selected[a]] \in (IF operation[a] = "Finish" THEN {"STARTED"} ELSE Nonterminal)
            /\ ((Safe \/ operation[a] = "Reconcile") => pending = selected[a])
Read(a) == /\ pc[a] = "Read" /\ Valid(a)
           /\ pc' = [pc EXCEPT ![a] = "Write"]
           /\ UNCHANGED <<pending, terminal, status, admitted, settled, nextId, owner,
                            selected, operation, outcome>>
Write(a) == /\ pc[a] = "Write"
            /\ status' = [status EXCEPT ![selected[a]] = outcome[a]]
            /\ settled' = IF outcome[a] \in Terminal /\ settled[selected[a]] = "ABSENT"
                           THEN [settled EXCEPT ![selected[a]] = outcome[a]] ELSE settled
            /\ pc' = [pc EXCEPT ![a] = IF outcome[a] = "UNKNOWN" THEN "Done" ELSE "Publish"]
            /\ UNCHANGED <<pending, terminal, admitted, nextId, owner, selected, operation, outcome>>
Publish(a) == /\ pc[a] = "Publish"
              /\ terminal' = selected[a]
              /\ pc' = [pc EXCEPT ![a] = "Unlink"]
              /\ UNCHANGED <<pending, status, admitted, settled, nextId, owner,
                               selected, operation, outcome>>
Unlink(a) == /\ pc[a] = "Unlink" /\ pending' = 0
             /\ pc' = [pc EXCEPT ![a] = "Done"]
             /\ UNCHANGED <<terminal, status, admitted, settled, nextId, owner,
                              selected, operation, outcome>>
Release(a) == /\ (pc[a] = "Done" \/ (pc[a] = "BeginCheck" /\ pending # 0)
                  \/ (pc[a] = "Read" /\ ~Valid(a)))
              /\ owner' = IF Safe THEN 0 ELSE owner
              /\ pc' = [pc EXCEPT ![a] = "Idle"]
              /\ UNCHANGED <<pending, terminal, status, admitted, settled, nextId,
                               selected, operation, outcome>>
Crash(a) == /\ pc[a] # "Idle"
            /\ owner' = IF Safe THEN 0 ELSE owner
            /\ pc' = [pc EXCEPT ![a] = "Idle"]
            /\ UNCHANGED <<pending, terminal, status, admitted, settled, nextId,
                             selected, operation, outcome>>
Next == \E a \in Actors :
          StartBegin(a) \/ BeginClaim(a) \/ BeginPersist(a) \/ BeginReturn(a)
          \/ (\E i \in Ids, op \in {"Finish", "Reconcile"}, result \in Terminal \cup {"UNKNOWN"}:
                StartTransition(a, i, op, result))
          \/ Read(a) \/ Write(a) \/ Publish(a) \/ Unlink(a) \/ Release(a) \/ Crash(a)
TypeOK == /\ pending \in Ids \cup {0} /\ terminal \in Ids \cup {0}
          /\ status \in [Ids -> Nonterminal \cup Terminal \cup {"ABSENT"}]
          /\ admitted \subseteq Ids /\ owner \in Actors \cup {0}
SingleUnresolved == Cardinality({i \in admitted : status[i] \in Nonterminal}) <= 1
PendingCoversUnresolved == \A i \in admitted : status[i] \in Nonterminal => pending = i
TerminalImmutable == \A i \in Ids : settled[i] \in Terminal => status[i] = settled[i]
Spec == Init /\ [][Next]_vars
=============================================================================
