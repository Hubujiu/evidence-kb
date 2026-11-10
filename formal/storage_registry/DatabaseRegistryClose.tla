----------------------- MODULE DatabaseRegistryClose -----------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS AllowDuplicateCleanup, KeepAdmissionOpen
Routes == {1, 2}
VARIABLES service, owned, phase, cleaners, leased, snapshot, closeStarted,
          lateUsed, failed
vars == <<service, owned, phase, cleaners, leased, snapshot, closeStarted,
          lateUsed, failed>>
Init == /\ service = "active"
        /\ owned = {1}
        /\ phase = [r \in Routes |-> IF r = 1 THEN "active" ELSE "absent"]
        /\ cleaners = [r \in Routes |-> 0]
        /\ leased = [r \in Routes |-> FALSE]
        /\ snapshot = {}
        /\ closeStarted = FALSE
        /\ lateUsed = FALSE
        /\ failed = {}
RegisterLate ==
    /\ service = "active" /\ ~lateUsed
    /\ owned' = owned \cup {2}
    /\ phase' = [phase EXCEPT ![2] = "active"]
    /\ lateUsed' = TRUE
    /\ UNCHANGED <<service, cleaners, leased, snapshot, closeStarted, failed>>
Acquire(r) ==
    /\ service = "active" /\ r \in owned
    /\ phase[r] = "active" /\ ~leased[r]
    /\ leased' = [leased EXCEPT ![r] = TRUE]
    /\ UNCHANGED <<service, owned, phase, cleaners, snapshot, closeStarted, lateUsed, failed>>
Release(r) ==
    /\ leased[r]
    /\ leased' = [leased EXCEPT ![r] = FALSE]
    /\ UNCHANGED <<service, owned, phase, cleaners, snapshot, closeStarted, lateUsed, failed>>
DrainBusy(r) ==
    /\ r \in owned /\ leased[r] /\ phase[r] = "active"
    /\ phase' = [phase EXCEPT ![r] = "draining"]
    /\ UNCHANGED <<service, owned, cleaners, leased, snapshot, closeStarted, lateUsed, failed>>
BeginClose ==
    /\ service = "active" /\ ~closeStarted
    /\ \A r \in owned: ~leased[r]
    /\ service' = IF KeepAdmissionOpen THEN "active" ELSE "draining"
    /\ snapshot' = owned
    /\ closeStarted' = TRUE
    /\ UNCHANGED <<owned, phase, cleaners, leased, lateUsed, failed>>
BeginDispose(r) ==
    /\ r \in owned /\ ~leased[r]
    /\ (phase[r] # "closing" \/ AllowDuplicateCleanup)
    /\ cleaners[r] < 2
    /\ phase' = [phase EXCEPT ![r] = "closing"]
    /\ cleaners' = [cleaners EXCEPT ![r] = @ + 1]
    /\ UNCHANGED <<service, owned, leased, snapshot, closeStarted, lateUsed, failed>>
CleanupFails(r) ==
    /\ r \in owned /\ cleaners[r] = 1 /\ r \notin failed
    /\ cleaners' = [cleaners EXCEPT ![r] = 0]
    /\ phase' = [phase EXCEPT ![r] = "cleanup_failed"]
    /\ failed' = failed \cup {r}
    /\ UNCHANGED <<service, owned, leased, snapshot, closeStarted, lateUsed>>
CleanupCompletes(r) ==
    /\ r \in owned /\ cleaners[r] = 1
    /\ cleaners' = [cleaners EXCEPT ![r] = 0]
    /\ phase' = [phase EXCEPT ![r] = "absent"]
    /\ owned' = owned \ {r}
    /\ UNCHANGED <<service, leased, snapshot, closeStarted, lateUsed, failed>>
FinishClose ==
    /\ closeStarted /\ service # "closed" /\ snapshot \cap owned = {}
    /\ service' = "closed"
    /\ UNCHANGED <<owned, phase, cleaners, leased, snapshot, closeStarted, lateUsed, failed>>
Next == \/ RegisterLate \/ BeginClose \/ FinishClose
        \/ \E r \in Routes: Acquire(r) \/ Release(r) \/ DrainBusy(r)
               \/ BeginDispose(r) \/ CleanupFails(r) \/ CleanupCompletes(r)
Spec == Init /\ [][Next]_vars
TypeOK == /\ service \in {"active", "draining", "closed"}
          /\ owned \subseteq Routes /\ snapshot \subseteq Routes /\ failed \subseteq Routes
          /\ phase \in [Routes -> {"absent", "active", "draining", "closing", "cleanup_failed"}]
          /\ cleaners \in [Routes -> 0..2] /\ leased \in [Routes -> BOOLEAN]
          /\ closeStarted \in BOOLEAN /\ lateUsed \in BOOLEAN
NoConcurrentCleanup == \A r \in Routes: cleaners[r] <= 1
NoCleanupOfLeased == \A r \in Routes: cleaners[r] > 0 => ~leased[r]
ClosedOwnsNothing == service = "closed" => owned = {} /\ \A r \in Routes: cleaners[r] = 0
ClosingBlocksAdmission == closeStarted => service # "active"
FailedCleanupRetained == \A r \in Routes: phase[r] = "cleanup_failed"
                        => r \in owned /\ cleaners[r] = 0
=============================================================================
