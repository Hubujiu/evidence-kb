------------------------- MODULE LlmRegistryCleanup -------------------------
EXTENDS Naturals, FiniteSets
CONSTANT AllowConcurrentCleanup
VARIABLES service, route, call, cleaners, failed, cleanupAttempt, acknowledged
vars == <<service, route, call, cleaners, failed, cleanupAttempt, acknowledged>>
Callers == 1..2
Init == /\ service = "active" /\ route = "active" /\ call = "idle"
        /\ cleaners = {} /\ failed = FALSE /\ cleanupAttempt = 0 /\ acknowledged = FALSE
Admit == /\ service = "active" /\ route = "active" /\ call = "idle"
         /\ call' = "admitted"
         /\ UNCHANGED <<service, route, cleaners, failed, cleanupAttempt, acknowledged>>
Complete == /\ call = "admitted" /\ call' = "completed"
            /\ UNCHANGED <<service, route, cleaners, failed, cleanupAttempt, acknowledged>>
Release == /\ call \in {"admitted", "completed"} /\ call' = "released"
           /\ UNCHANGED <<service, route, cleaners, failed, cleanupAttempt, acknowledged>>
BusyDrain == /\ route \in {"active", "draining"} /\ call \in {"admitted", "completed"}
             /\ route' = "draining"
             /\ UNCHANGED <<service, call, cleaners, failed, cleanupAttempt, acknowledged>>
BeginCleanup(c) == /\ route # "absent" /\ call \notin {"admitted", "completed"}
                   /\ (route # "cleaning" \/ AllowConcurrentCleanup)
                   /\ c \notin cleaners /\ cleanupAttempt < 2
                   /\ cleaners' = cleaners \cup {c} /\ route' = "cleaning"
                   /\ cleanupAttempt' = cleanupAttempt + 1
                   /\ UNCHANGED <<service, call, failed, acknowledged>>
CleanupFails(c) == /\ c \in cleaners /\ ~failed
                   /\ cleaners' = cleaners \ {c} /\ route' = "cleanupFailed" /\ failed' = TRUE
                   /\ UNCHANGED <<service, call, cleanupAttempt, acknowledged>>
CleanupSucceeds(c) == /\ c \in cleaners
                      /\ cleaners' = cleaners \ {c} /\ route' = "absent" /\ acknowledged' = TRUE
                      /\ UNCHANGED <<service, call, failed, cleanupAttempt>>
BeginClose == /\ call \notin {"admitted", "completed"} /\ service = "active"
              /\ service' = "draining"
              /\ UNCHANGED <<route, call, cleaners, failed, cleanupAttempt, acknowledged>>
FinishClose == /\ route = "absent" /\ service # "closed" /\ service' = "closed"
               /\ UNCHANGED <<route, call, cleaners, failed, cleanupAttempt, acknowledged>>
Next == Admit \/ Complete \/ Release \/ BusyDrain \/ BeginClose \/ FinishClose
        \/ (\E c \in Callers : BeginCleanup(c) \/ CleanupFails(c) \/ CleanupSucceeds(c))
TypeOK == /\ service \in {"active", "draining", "closed"}
          /\ route \in {"active", "draining", "cleaning", "cleanupFailed", "absent"}
          /\ call \in {"idle", "admitted", "completed", "released"}
          /\ cleaners \subseteq Callers /\ failed \in BOOLEAN
          /\ cleanupAttempt \in 0..2 /\ acknowledged \in BOOLEAN
ExclusiveCleanup == Cardinality(cleaners) <= 1
NoCleanupWithAdmission == call \in {"admitted", "completed"} => cleaners = {}
ClosedOwnsNothing == service = "closed" => (route = "absent" /\ cleaners = {})
NoUnacknowledgedRemoval == route = "absent" => acknowledged
FailureRetainsEntry == failed /\ ~acknowledged => route # "absent"
Spec == Init /\ [][Next]_vars
=============================================================================
