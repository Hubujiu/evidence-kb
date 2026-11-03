--------------------------- MODULE PluginLifecycle ---------------------------
EXTENDS Naturals, FiniteSets, TLC

(* Coordinator abstraction: P provides the dependency retained by C. Init
   actions are claimed. TRUE reproduces main 8160c05's superseded-error bug. *)
CONSTANT PreserveSupersededFailure
Plugins == {"P", "C"}
Phases == {"Pending", "Starting", "Active", "Draining", "Cleaning",
           "CleanupFailed", "Failed", "Disposed"}
Busy(call) == call \in {"running", "cancelled"}
VARIABLES phase, generation, desired, initializing, failure, resources,
          calls, binding, nextManifest, updated, cleanupFaultUsed, lateError
vars == <<phase, generation, desired, initializing, failure, resources,
          calls, binding, nextManifest, updated, cleanupFaultUsed, lateError>>

Init ==
    /\ phase = [p \in Plugins |-> IF p = "P" THEN "Active" ELSE "Starting"]
    /\ generation = [p \in Plugins |-> 1]
    /\ desired = [p \in Plugins |-> "enabled"]
    /\ initializing = [p \in Plugins |-> p = "C"]
    /\ failure = [p \in Plugins |-> "none"]
    /\ resources = [p \in Plugins |-> 1]
    /\ calls = [p \in Plugins |-> "none"]
    /\ binding = 1
    /\ nextManifest = [p \in Plugins |-> FALSE]
    /\ updated = FALSE
    /\ cleanupFaultUsed = [p \in Plugins |-> FALSE]
    /\ lateError = FALSE

UpdateProvider ==
    /\ ~updated /\ desired["P"] = "enabled" /\ phase["P"] = "Active"
    /\ phase' = [p \in Plugins |-> IF phase[p] \in {"Starting", "Active"}
                                   THEN "Draining" ELSE phase[p]]
    /\ nextManifest' = [nextManifest EXCEPT !["P"] = TRUE]
    /\ calls' = [p \in Plugins |-> IF calls[p] = "running"
                                   THEN "cancelled" ELSE calls[p]]
    /\ updated' = TRUE
    /\ UNCHANGED <<generation, desired, initializing, failure, resources,
                   binding, cleanupFaultUsed, lateError>>

RemoveScope ==
    /\ desired["P"] = "enabled"
    /\ desired' = [p \in Plugins |-> "removed"]
    /\ phase' = [p \in Plugins |->
          CASE phase[p] \in {"Starting", "Active"} -> "Draining"
            [] phase[p] \in {"Pending", "Failed"} -> "Disposed"
            [] OTHER -> phase[p]]
    /\ calls' = [p \in Plugins |-> IF calls[p] = "running"
                                   THEN "cancelled" ELSE calls[p]]
    /\ UNCHANGED <<generation, initializing, failure, resources, binding,
                   nextManifest, updated, cleanupFaultUsed, lateError>>

RegisterResource(p) ==
    /\ initializing[p] /\ resources[p] < 2
    /\ resources' = [resources EXCEPT ![p] = @ + 1]
    /\ UNCHANGED <<phase, generation, desired, initializing, failure, calls,
                   binding, nextManifest, updated, cleanupFaultUsed, lateError>>

CompleteInit(p, success) ==
    /\ initializing[p]
    (* Only first consumer init may fail; successor work succeeds for liveness. *)
    /\ success \/ (p = "C" /\ generation[p] = 1)
    /\ initializing' = [initializing EXCEPT ![p] = FALSE]
    /\ phase' = [phase EXCEPT ![p] =
          IF success /\ phase[p] = "Starting" THEN "Active" ELSE "Draining"]
    /\ failure' = [failure EXCEPT ![p] =
          IF ~success /\ phase[p] = "Starting" THEN "current"
          ELSE IF ~success /\ PreserveSupersededFailure THEN "superseded"
          ELSE @]
    /\ lateError' = (lateError \/ (~success /\ phase[p] = "Draining"))
    /\ UNCHANGED <<generation, desired, resources, calls, binding,
                   nextManifest, updated, cleanupFaultUsed>>

StartInit(p) ==
    /\ phase[p] = "Pending" /\ desired[p] = "enabled"
    /\ p = "P" \/ phase["P"] = "Active"
    /\ phase' = [phase EXCEPT ![p] = "Starting"]
    /\ initializing' = [initializing EXCEPT ![p] = TRUE]
    /\ binding' = IF p = "C" THEN generation["P"] ELSE binding
    /\ UNCHANGED <<generation, desired, failure, resources, calls,
                   nextManifest, updated, cleanupFaultUsed, lateError>>

Invoke(p) ==
    /\ phase[p] = "Active" /\ desired[p] = "enabled" /\ calls[p] = "none"
    /\ calls' = [calls EXCEPT ![p] = "running"]
    /\ UNCHANGED <<phase, generation, desired, initializing, failure, resources,
                   binding, nextManifest, updated, cleanupFaultUsed, lateError>>

CancelCall(p) ==
    /\ calls[p] = "running"
    /\ calls' = [calls EXCEPT ![p] = "cancelled"]
    /\ UNCHANGED <<phase, generation, desired, initializing, failure, resources,
                   binding, nextManifest, updated, cleanupFaultUsed, lateError>>

CompleteCall(p) ==
    /\ Busy(calls[p])
    /\ calls' = [calls EXCEPT ![p] = "complete"]
    /\ UNCHANGED <<phase, generation, desired, initializing, failure, resources,
                   binding, nextManifest, updated, cleanupFaultUsed, lateError>>

BeginCleanup(p) ==
    /\ phase[p] = "Draining" /\ ~initializing[p] /\ ~Busy(calls[p])
    /\ p = "C" \/ binding = 0
    /\ desired[p] # "removed" \/ \A owner \in Plugins : ~Busy(calls[owner])
    /\ phase' = [phase EXCEPT ![p] = "Cleaning"]
    /\ UNCHANGED <<generation, desired, initializing, failure, resources, calls,
                   binding, nextManifest, updated, cleanupFaultUsed, lateError>>

CleanupError(p) ==
    /\ phase[p] = "Cleaning" /\ ~cleanupFaultUsed[p]
    /\ phase' = [phase EXCEPT ![p] = "CleanupFailed"]
    /\ cleanupFaultUsed' = [cleanupFaultUsed EXCEPT ![p] = TRUE]
    /\ UNCHANGED <<generation, desired, initializing, failure, resources, calls,
                   binding, nextManifest, updated, lateError>>

RetryCleanup(p) ==
    /\ phase[p] = "CleanupFailed"
    /\ phase' = [phase EXCEPT ![p] = "Cleaning"]
    /\ UNCHANGED <<generation, desired, initializing, failure, resources, calls,
                   binding, nextManifest, updated, cleanupFaultUsed, lateError>>

CleanupResource(p) ==
    /\ phase[p] = "Cleaning" /\ resources[p] > 0
    /\ resources' = [resources EXCEPT ![p] = @ - 1]
    /\ UNCHANGED <<phase, generation, desired, initializing, failure, calls,
                   binding, nextManifest, updated, cleanupFaultUsed, lateError>>

FinishCleanup(p) ==
    /\ phase[p] = "Cleaning" /\ resources[p] = 0
    /\ LET failed == failure[p] # "none" /\ ~nextManifest[p]
           target == IF desired[p] = "removed" THEN "Disposed"
                     ELSE IF failed THEN "Failed" ELSE "Pending"
       IN /\ phase' = [phase EXCEPT ![p] = target]
          /\ generation' = [generation EXCEPT ![p] =
                 IF target = "Pending" THEN @ + 1 ELSE @]
          /\ failure' = [failure EXCEPT ![p] =
                 IF nextManifest[p] THEN "none" ELSE @]
    /\ binding' = IF p = "C" THEN 0 ELSE binding
    /\ nextManifest' = [nextManifest EXCEPT ![p] = FALSE]
    /\ UNCHANGED <<desired, initializing, resources, calls,
                   updated, cleanupFaultUsed, lateError>>

Next == UpdateProvider \/ RemoveScope \/
    \E p \in Plugins : RegisterResource(p) \/
        (\E success \in BOOLEAN : CompleteInit(p, success)) \/ StartInit(p) \/
        Invoke(p) \/ CancelCall(p) \/ CompleteCall(p) \/ BeginCleanup(p) \/
        CleanupError(p) \/ RetryCleanup(p) \/ CleanupResource(p) \/ FinishCleanup(p)

Spec == Init /\ [][Next]_vars /\
    \A p \in Plugins :
        WF_vars(CompleteInit(p, TRUE)) /\ WF_vars(StartInit(p)) /\
        WF_vars(CompleteCall(p)) /\ WF_vars(BeginCleanup(p)) /\
        WF_vars(RetryCleanup(p)) /\ WF_vars(CleanupResource(p)) /\
        WF_vars(FinishCleanup(p))

TypeOK ==
    /\ phase \in [Plugins -> Phases]
    /\ generation \in [Plugins -> 1..2]
    /\ desired \in [Plugins -> {"enabled", "removed"}]
    /\ initializing \in [Plugins -> BOOLEAN]
    /\ failure \in [Plugins -> {"none", "current", "superseded"}]
    /\ resources \in [Plugins -> 0..2]
    /\ calls \in [Plugins -> {"none", "running", "cancelled", "complete"}]
    /\ binding \in 0..2
    /\ nextManifest \in [Plugins -> BOOLEAN]
    /\ cleanupFaultUsed \in [Plugins -> BOOLEAN]
    /\ updated \in BOOLEAN /\ lateError \in BOOLEAN
ActiveDependencies == phase["C"] = "Active" =>
    phase["P"] = "Active" /\ binding = generation["P"]
CleanupWaitsForRealCompletion == \A p \in Plugins :
    phase[p] \in {"Cleaning", "CleanupFailed", "Disposed"} =>
        ~initializing[p] /\ ~Busy(calls[p])
ProviderRetainedForConsumer == binding # 0 =>
    generation["P"] = binding /\
    phase["P"] \notin {"Cleaning", "CleanupFailed", "Disposed"}
DisposedOwnsNothing == \A p \in Plugins :
    phase[p] = "Disposed" => resources[p] = 0
NoSupersededFailure == \A p \in Plugins : failure[p] # "superseded"
CurrentFailureRemainsTerminal == \A p \in Plugins :
    failure[p] = "current" /\ phase[p] \notin {"Draining", "Cleaning", "CleanupFailed"}
        => phase[p] \in {"Failed", "Disposed"}
SupersededInitEventuallyRecovers ==
    lateError ~> (phase["C"] = "Active" \/ desired["C"] = "removed")
RemovedScopeEventuallyDisposes ==
    (desired["P"] = "removed") ~> (\A p \in Plugins : phase[p] = "Disposed")
=============================================================================
