------------------------ MODULE ScopeLeaseAdmission ------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS EnforceSerial, EnforceScope, PinLiveLeases, RetainDependencies
VARIABLES live, allocated, ancestors, execution, cancelled, closing, disposed,
          bindings, now, nestedAfterCancel, foreignRejected, badForeign
vars == <<live, allocated, ancestors, execution, cancelled, closing, disposed,
          bindings, now, nestedAfterCancel, foreignRejected, badForeign>>
Leases == 1..4
Owners == {"parent", "second", "consumer"}
Init == /\ live = {} /\ allocated = {} /\ execution = {}
        /\ ancestors = [l \in Leases |-> {}] /\ cancelled = {}
        /\ closing = FALSE /\ disposed = {} /\ bindings = {"parent", "second"}
        /\ now = 0 /\ nestedAfterCancel = FALSE /\ foreignRejected = FALSE /\ badForeign = FALSE
ScopePin == /\ 1 \notin allocated /\ ~closing /\ now < 2
            /\ live' = live \cup {1} /\ allocated' = allocated \cup {1}
            /\ UNCHANGED <<ancestors, execution, cancelled, closing, disposed, bindings, now,
                           nestedAfterCancel, foreignRejected, badForeign>>
Call(l, parent) ==
    /\ l \in 2..4 /\ l \notin allocated /\ parent \in live \cup {0}
    /\ parent = 0 => ~closing
    /\ now < 2 /\ disposed = {}
    /\ LET chain == IF parent = 0 THEN {} ELSE ancestors[parent] \cup {parent}
       IN /\ (~EnforceSerial \/ execution \cap live \subseteq chain)
          /\ ancestors' = [ancestors EXCEPT ![l] = chain]
    /\ live' = live \cup {l} /\ allocated' = allocated \cup {l}
    /\ execution' = execution \cup {l}
    /\ nestedAfterCancel' = (nestedAfterCancel \/ parent \in cancelled)
    /\ UNCHANGED <<cancelled, closing, disposed, bindings, now, foreignRejected, badForeign>>
ForeignCall == /\ ~foreignRejected /\ ~badForeign /\ ~closing /\ now < 2
               /\ foreignRejected' = EnforceScope /\ badForeign' = ~EnforceScope
               /\ UNCHANGED <<live, allocated, ancestors, execution, cancelled, closing,
                              disposed, bindings, now, nestedAfterCancel>>
Cancel(l) == /\ l \in live \cap execution /\ l \notin cancelled
             /\ cancelled' = cancelled \cup {l}
             /\ UNCHANGED <<live, allocated, ancestors, execution, closing, disposed, bindings,
                            now, nestedAfterCancel, foreignRejected, badForeign>>
Tick == /\ now < 2 /\ now' = now + 1
        /\ cancelled' = (IF now' = 2 THEN cancelled \cup (live \cap execution) ELSE cancelled)
        /\ UNCHANGED <<live, allocated, ancestors, execution, closing, disposed, bindings,
                       nestedAfterCancel, foreignRejected, badForeign>>
Complete(l) == /\ l \in live /\ live' = live \ {l}
               /\ UNCHANGED <<allocated, ancestors, execution, cancelled, closing, disposed,
                              bindings, now, nestedAfterCancel, foreignRejected, badForeign>>
Drain == /\ ~closing /\ closing' = TRUE /\ cancelled' = cancelled \cup live
         /\ UNCHANGED <<live, allocated, ancestors, execution, disposed, bindings, now,
                        nestedAfterCancel, foreignRejected, badForeign>>
CleanConsumer == /\ closing /\ "consumer" \notin disposed
                 /\ (~PinLiveLeases \/ live = {})
                 /\ disposed' = disposed \cup {"consumer"} /\ bindings' = {}
                 /\ UNCHANGED <<live, allocated, ancestors, execution, cancelled, closing, now,
                                nestedAfterCancel, foreignRejected, badForeign>>
CleanProvider(owner) == /\ owner \in {"parent", "second"} \ disposed /\ closing
                        /\ (~PinLiveLeases \/ live = {})
                        /\ (~RetainDependencies \/ owner \notin bindings)
                        /\ disposed' = disposed \cup {owner}
                        /\ UNCHANGED <<live, allocated, ancestors, execution, cancelled, closing,
                                       bindings, now, nestedAfterCancel, foreignRejected, badForeign>>
Next == ScopePin \/ (\E l \in 2..4, parent \in Leases \cup {0} : Call(l, parent))
        \/ ForeignCall \/ (\E l \in Leases : Cancel(l) \/ Complete(l)) \/ Tick \/ Drain
        \/ CleanConsumer \/ (\E owner \in {"parent", "second"} : CleanProvider(owner))
Spec == Init /\ [][Next]_vars
TypeOK == /\ live \subseteq allocated /\ allocated \subseteq Leases
          /\ execution \subseteq allocated /\ cancelled \subseteq allocated
          /\ ancestors \in [Leases -> SUBSET Leases]
          /\ disposed \subseteq Owners /\ bindings \subseteq {"parent", "second"}
          /\ closing \in BOOLEAN /\ now \in 0..2 /\ nestedAfterCancel \in BOOLEAN
          /\ foreignRejected \in BOOLEAN /\ badForeign \in BOOLEAN
ImmutableLeaseAncestry == \A l \in allocated : l \notin ancestors[l] /\ ancestors[l] \subseteq allocated
SerialChainOnly == \A a,b \in live \cap execution : a = b \/ a \in ancestors[b] \/ b \in ancestors[a]
NoForeignOwner == ~badForeign
NoEarlyCleanup == live # {} => disposed = {}
DependenciesRetained == disposed \cap bindings = {}
NestedCancelSuccessUnreachable == ~nestedAfterCancel
DrainSuccessUnreachable == ~(nestedAfterCancel /\ disposed = Owners /\ live = {} /\ foreignRejected)
=============================================================================
