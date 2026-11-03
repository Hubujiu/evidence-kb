-------------------------- MODULE StorageShutdown --------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS EarlyAck, EarlyPop, AllowReentry
ASSUME /\ EarlyAck \in BOOLEAN /\ EarlyPop \in BOOLEAN /\ AllowReentry \in BOOLEAN

VARIABLES closing, active, endpoints, providers, call, acknowledged, physical,
          attempts, failed, overlap, done
vars == <<closing, active, endpoints, providers, call, acknowledged, physical,
          attempts, failed, overlap, done>>

Init ==
    /\ closing = FALSE /\ active = 0 /\ endpoints = 1 /\ providers = 2
    /\ call = 0 /\ acknowledged = {} /\ physical = {}
    /\ attempts = [p \in 1..2 |-> 0]
    /\ failed = FALSE /\ overlap = FALSE /\ done = FALSE

Admit ==
    /\ ~closing /\ active < 2 /\ active' = active + 1
    /\ UNCHANGED <<closing, endpoints, providers, call, acknowledged, physical,
          attempts, failed, overlap, done>>

Leave ==
    /\ active > 0 /\ active' = active - 1
    /\ UNCHANGED <<closing, endpoints, providers, call, acknowledged, physical,
          attempts, failed, overlap, done>>

BeginClose ==
    /\ ~closing /\ closing' = TRUE
    /\ UNCHANGED <<active, endpoints, providers, call, acknowledged, physical,
          attempts, failed, overlap, done>>

CloseEndpoint ==
    /\ closing /\ active = 0 /\ endpoints = 1 /\ endpoints' = 0
    /\ UNCHANGED <<closing, active, providers, call, acknowledged, physical,
          attempts, failed, overlap, done>>

StartPhysical ==
    /\ closing /\ active = 0 /\ endpoints = 0 /\ providers > 0 /\ call = 0
    /\ providers \notin acknowledged /\ attempts[providers] < 2
    /\ call' = providers
    /\ attempts' = [attempts EXCEPT ![providers] = @ + 1]
    /\ acknowledged' = IF EarlyAck THEN acknowledged \cup {providers} ELSE acknowledged
    /\ providers' = IF EarlyPop THEN providers - 1 ELSE providers
    /\ UNCHANGED <<closing, active, endpoints, physical, failed, overlap, done>>

PhysicalFail ==
    /\ call > 0 /\ attempts[call] = 1
    /\ call' = 0 /\ failed' = TRUE
    /\ UNCHANGED <<closing, active, endpoints, providers, acknowledged, physical,
          attempts, overlap, done>>

PhysicalSuccess ==
    /\ call > 0
    /\ physical' = physical \cup {call}
    /\ acknowledged' = acknowledged \cup {call}
    /\ call' = 0
    /\ UNCHANGED <<closing, active, endpoints, providers, attempts, failed, overlap, done>>

PopSuccessful ==
    /\ call = 0 /\ providers > 0 /\ providers \in acknowledged
    /\ providers' = providers - 1
    /\ UNCHANGED <<closing, active, endpoints, call, acknowledged, physical,
          attempts, failed, overlap, done>>

Reenter ==
    /\ call > 0 /\ AllowReentry /\ overlap' = TRUE
    /\ UNCHANGED <<closing, active, endpoints, providers, call, acknowledged,
          physical, attempts, failed, done>>

Finish ==
    /\ closing /\ active = 0 /\ endpoints = 0 /\ providers = 0 /\ call = 0
    /\ done' = TRUE
    /\ UNCHANGED <<closing, active, endpoints, providers, call, acknowledged,
          physical, attempts, failed, overlap>>

Next == Admit \/ Leave \/ BeginClose \/ CloseEndpoint \/ StartPhysical
        \/ PhysicalFail \/ PhysicalSuccess \/ PopSuccessful \/ Reenter \/ Finish
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ closing \in BOOLEAN /\ active \in 0..2 /\ endpoints \in 0..1
    /\ providers \in 0..2 /\ call \in 0..2
    /\ acknowledged \subseteq 1..2 /\ physical \subseteq 1..2
    /\ attempts \in [1..2 -> 0..2]
    /\ failed \in BOOLEAN /\ overlap \in BOOLEAN /\ done \in BOOLEAN
AcknowledgedCleanup == acknowledged \subseteq physical
RetainedUntilClean == {p \in 1..2 : p > providers} \subseteq physical
NoCleanupDuringOperation == call > 0 => active = 0
NoReentrantCleanup == ~overlap
CompleteOwnsNothing == done => /\ providers = 0 /\ endpoints = 0 /\ physical = 1..2
NoRetryWitness == ~(done /\ failed /\ attempts[1] = 2 /\ attempts[2] = 2)
NoBusyDrainWitness == ~(closing /\ active = 2 /\ endpoints = 1 /\ providers = 2)
=============================================================================
