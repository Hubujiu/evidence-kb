------------------------- MODULE SubtreeCollection -------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS FollowStoredParents, GuardBusy, CleanCallHosts, CleanReferences,
          PreserveForeign, FreshIdentity
VARIABLES live, closing, disposed, edges, work, pin, records, affected,
          diagnostics, collected, allocated, created, early, reused, rejected,
          childDone, rootDone
vars == <<live, closing, disposed, edges, work, pin, records, affected,
          diagnostics, collected, allocated, created, early, reused, rejected,
          childDone, rootDone>>
Hosts == 1..5
Parent == [h \in Hosts |-> CASE h = 2 -> 1 [] h = 3 -> 2 [] OTHER -> 0]
OpHost == [o \in 1..5 |-> IF o = 5 THEN 3 ELSE o]
OpOwner == [o \in 1..5 |-> IF o = 5 THEN 1 ELSE o]
Desc(t) == IF t = 1 THEN {1,2,3} ELSE {2,3}
LogicalDesc(t) == {t} \cup {h \in live : <<t,h>> \in edges}
                 \cup {h \in live : \E mid \in live : <<t,mid>> \in edges /\ <<mid,h>> \in edges}
Busy(hs) == ~(hs \subseteq disposed) \/ (3 \in hs /\ work) \/ (2 \in hs /\ pin)
Init == /\ live = 1..4 /\ closing = {} /\ disposed = {}
        /\ edges = {<<1,2>>, <<2,3>>} /\ work = TRUE /\ pin = TRUE
        /\ records = 1..5 /\ affected = {2,3} /\ diagnostics = 1..4
        /\ collected = {} /\ allocated = 1..4 /\ created = FALSE
        /\ early = FALSE /\ reused = FALSE /\ rejected = FALSE
        /\ childDone = FALSE /\ rootDone = FALSE
Drain(t) == /\ t \in {1,2} \cap live /\ ~(t \in closing)
            /\ closing' = closing \cup (Desc(t) \cap live)
            /\ UNCHANGED <<live, disposed, edges, work, pin, records, affected,
                           diagnostics, collected, allocated, created, early,
                           reused, rejected, childDone, rootDone>>
Complete == /\ work /\ work' = FALSE
            /\ UNCHANGED <<live, closing, disposed, edges, pin, records, affected,
                           diagnostics, collected, allocated, created, early,
                           reused, rejected, childDone, rootDone>>
Release == /\ pin /\ pin' = FALSE
           /\ UNCHANGED <<live, closing, disposed, edges, work, records, affected,
                          diagnostics, collected, allocated, created, early,
                          reused, rejected, childDone, rootDone>>
Dispose(h) == /\ h \in closing \cap live /\ h \notin disposed
              /\ ~\E c \in live : <<h,c>> \in edges
              /\ (h # 2 \/ ~pin) /\ (h \notin {1,3} \/ ~work)
              /\ disposed' = disposed \cup {h}
              /\ edges' = {e \in edges : e[2] # h}
              /\ UNCHANGED <<live, closing, work, pin, records, affected,
                             diagnostics, collected, allocated, created, early,
                             reused, rejected, childDone, rootDone>>
Reject(t) == /\ t \in {1,2} \cap live /\ Busy(Desc(t) \cap live) /\ ~rejected
             /\ rejected' = TRUE
             /\ UNCHANGED <<live, closing, disposed, edges, work, pin, records,
                            affected, diagnostics, collected, allocated, created,
                            early, reused, childDone, rootDone>>
Collect(t) ==
    /\ t \in {1,2} \cap live
    /\ LET wanted == Desc(t) \cap live
           hs == (IF FollowStoredParents THEN wanted ELSE LogicalDesc(t))
                 \cup (IF PreserveForeign THEN {} ELSE {4})
       IN /\ (~GuardBusy \/ ~Busy(wanted))
          /\ live' = live \ hs /\ closing' = closing \ hs /\ disposed' = disposed \ hs
          /\ edges' = {e \in edges : e[1] \notin hs /\ e[2] \notin hs}
          /\ records' = {o \in records : OpOwner[o] \notin hs /\ (~CleanCallHosts \/ OpHost[o] \notin hs)}
          /\ affected' = (IF CleanReferences THEN affected \ hs ELSE affected)
          /\ diagnostics' = (IF CleanReferences THEN diagnostics \ hs ELSE diagnostics)
          /\ collected' = collected \cup hs
          /\ early' = (early \/ Busy(hs))
          /\ childDone' = (childDone \/ (t = 2 /\ hs = {2,3} /\ rejected /\ ~work /\ ~pin))
          /\ rootDone' = (rootDone \/ (t = 1 /\ hs = {1,2,3} /\ rejected /\ ~work /\ ~pin))
    /\ UNCHANGED <<work, pin, allocated, created, reused, rejected>>
Create == /\ collected # {} /\ ~created
          /\ LET id == IF FreshIdentity THEN 5 ELSE 2
             IN /\ id \notin live /\ live' = live \cup {id}
                /\ allocated' = allocated \cup {id} /\ reused' = (reused \/ id \in allocated)
          /\ created' = TRUE
          /\ UNCHANGED <<closing, disposed, edges, work, pin, records, affected,
                         diagnostics, collected, early, rejected, childDone, rootDone>>
Next == (\E t \in {1,2} : Drain(t) \/ Reject(t) \/ Collect(t))
        \/ (\E h \in Hosts : Dispose(h)) \/ Complete \/ Release \/ Create
Spec == Init /\ [][Next]_vars
TypeOK == /\ live \subseteq Hosts /\ closing \subseteq live /\ disposed \subseteq live
          /\ edges \subseteq (Hosts \X Hosts) /\ records \subseteq 1..5
          /\ affected \subseteq {2,3} /\ diagnostics \subseteq 1..4
          /\ collected \subseteq 1..4 /\ allocated \subseteq Hosts
          /\ {work,pin,created,early,reused,rejected,childDone,rootDone} \subseteq BOOLEAN
NoEarlyCollection == ~early
StoredParentClosure == \A h \in live : Parent[h] = 0 \/ Parent[h] \in live
NoDanglingRecords == \A o \in records : OpHost[o] \in live /\ OpOwner[o] \in live
NoDanglingReferences == affected \subseteq live /\ diagnostics \subseteq live
ForeignSurvives == 4 \in live /\ 4 \in records /\ 4 \in diagnostics
NoIdentityReuse == ~reused
ChildCollectionSuccessUnreachable == ~(childDone /\ 1 \in live /\ 4 \in live /\ created /\ records = {1,4})
RootCollectionSuccessUnreachable == ~(rootDone /\ 4 \in live /\ created /\ records = {4})
=============================================================================
