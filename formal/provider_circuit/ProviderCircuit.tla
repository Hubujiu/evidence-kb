-------------------------- MODULE ProviderCircuit --------------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Requests, Threshold, Legacy, DuplicateFinishes
NoRequest == "none"
Outcomes == {"success", "failure", "neutral"}

VARIABLES opened, probe, generation, failures, cooldownReady,
          acquired, completed, permits, staleMutation, duplicateMutation

vars == <<opened, probe, generation, failures, cooldownReady,
          acquired, completed, permits, staleMutation, duplicateMutation>>
breaker == <<opened, probe, generation, failures, cooldownReady>>

Init ==
    /\ opened = FALSE
    /\ probe = NoRequest
    /\ generation = 0
    /\ failures = 0
    /\ cooldownReady = FALSE
    /\ acquired = {}
    /\ completed = {}
    /\ permits = [r \in Requests |-> [epoch |-> 0, probing |-> FALSE]]
    /\ staleMutation = FALSE
    /\ duplicateMutation = FALSE

Acquire(r) ==
    /\ r \notin acquired
    /\ ~opened \/ (cooldownReady /\ probe = NoRequest)
    /\ LET probing == opened
           nextGeneration == IF probing /\ ~Legacy THEN generation + 1 ELSE generation
       IN /\ generation' = nextGeneration
          /\ permits' = [permits EXCEPT ![r] = [epoch |-> nextGeneration, probing |-> probing]]
          /\ probe' = IF probing THEN r ELSE probe
    /\ acquired' = acquired \cup {r}
    /\ UNCHANGED <<opened, failures, cooldownReady, completed, staleMutation, duplicateMutation>>

Tick ==
    /\ opened
    /\ ~cooldownReady
    /\ cooldownReady' = TRUE
    /\ UNCHANGED <<opened, probe, generation, failures, acquired, completed,
                   permits, staleMutation, duplicateMutation>>

Finish(r, outcome) ==
    /\ r \in acquired
    /\ DuplicateFinishes \/ r \notin completed
    /\ LET accepted == Legacy \/ (r \notin completed /\ permits[r].epoch = generation)
           probing == permits[r].probing
           failedCount == IF failures < Threshold THEN failures + 1 ELSE Threshold
           opens == outcome = "failure" /\ (probing \/ failedCount >= Threshold)
           changesEpoch == probing \/ opens
       IN /\ IF accepted
             THEN /\ opened' = IF outcome = "success" THEN FALSE ELSE IF opens THEN TRUE ELSE opened
                  /\ failures' = CASE outcome = "success" -> 0
                                     [] outcome = "failure" -> failedCount
                                     [] OTHER -> failures
                  /\ probe' = IF probing THEN NoRequest ELSE probe
                  /\ cooldownReady' = IF opens THEN FALSE ELSE cooldownReady
                  /\ generation' = IF ~Legacy /\ changesEpoch THEN generation + 1 ELSE generation
             ELSE UNCHANGED breaker
          /\ staleMutation' = (staleMutation \/
                 (~Legacy /\ permits[r].epoch # generation /\ breaker' # breaker))
          /\ duplicateMutation' = (duplicateMutation \/
                 (r \in completed /\ breaker' # breaker))
    /\ completed' = completed \cup {r}
    /\ UNCHANGED <<acquired, permits>>

Next ==
    \/ \E r \in Requests : Acquire(r)
    \/ Tick
    \/ \E r \in Requests, outcome \in Outcomes : Finish(r, outcome)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ opened \in BOOLEAN
    /\ probe \in Requests \cup {NoRequest}
    /\ generation \in 0..(2 * Cardinality(Requests))
    /\ failures \in 0..Threshold
    /\ cooldownReady \in BOOLEAN
    /\ completed \subseteq acquired
    /\ acquired \subseteq Requests
    /\ permits \in [Requests -> [epoch : 0..(2 * Cardinality(Requests)), probing : BOOLEAN]]
    /\ staleMutation \in BOOLEAN
    /\ duplicateMutation \in BOOLEAN

ProbeRequiresOpen == probe # NoRequest => opened
ProbeHasCurrentOwner == probe # NoRequest =>
    /\ probe \in acquired \ completed
    /\ permits[probe].probing
    /\ Legacy \/ permits[probe].epoch = generation
ProbeExcludesAdmission == probe # NoRequest => ~\E r \in Requests : ENABLED Acquire(r)
NoStaleMutation == ~staleMutation
NoDuplicateMutation == ~duplicateMutation

Complete(r) == \E outcome \in Outcomes : Finish(r, outcome)
FairSpec == Spec /\ \A r \in Requests : WF_vars(Complete(r))
ProbeEventuallyReleased == \A r \in Requests : (probe = r) ~> (probe # r)
=============================================================================
