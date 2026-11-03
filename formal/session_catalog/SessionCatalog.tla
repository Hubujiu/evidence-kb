---- MODULE SessionCatalog ----
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS CheckTenant, CheckPrincipal, CheckContent, CheckSessionKey, CheckLease
Actors == {"owner", "other-principal", "other-tenant"}
Sessions == {1, 2}
Bodies == {1, 2}
Tenant(a) == IF a = "other-tenant" THEN 2 ELSE 1
Principal(a) == IF a = "other-principal" THEN 2 ELSE 1
Owner(s) == IF s = 1 THEN "owner" ELSE "other-principal"
Authorized(a, s) == Tenant(a) = Tenant(Owner(s)) /\ Principal(a) = Principal(Owner(s))
Admitted(a, s) == (~CheckTenant \/ Tenant(a) = Tenant(Owner(s))) /\ (~CheckPrincipal \/ Principal(a) = Principal(Owner(s)))
Keys == Sessions \X Bodies
EmptyObservation == [ok |-> FALSE, authorized |-> TRUE, content |-> TRUE, bound |-> TRUE, artifact |-> FALSE]
VARIABLES catalog, stored, phase, creating, createAck, createUnknown, observed,
          writeAuthorized, writeLease, dedupSeen
vars == <<catalog, stored, phase, creating, createAck, createUnknown, observed,
          writeAuthorized, writeLease, dedupSeen>>
Init == /\ catalog = {}
        /\ stored = [key \in Keys |-> "absent"]
        /\ phase = "idle" /\ creating = 1 /\ createAck = {} /\ createUnknown = {}
        /\ observed = EmptyObservation
        /\ writeAuthorized = TRUE /\ writeLease = TRUE /\ dedupSeen = FALSE
StartCreate(s) == /\ phase = "idle" /\ s \notin catalog
                  /\ phase' = "staged" /\ creating' = s
                  /\ UNCHANGED <<catalog, stored, createAck, createUnknown, observed, writeAuthorized, writeLease, dedupSeen>>
AbortCreate == /\ phase = "staged" /\ phase' = "idle"
               /\ UNCHANGED <<catalog, stored, creating, createAck, createUnknown, observed, writeAuthorized, writeLease, dedupSeen>>
CommitCreate == /\ phase = "staged" /\ catalog' = catalog \cup {creating} /\ phase' = "committed"
                /\ UNCHANGED <<stored, creating, createAck, createUnknown, observed, writeAuthorized, writeLease, dedupSeen>>
AcknowledgeCreate == /\ phase = "committed" /\ createAck' = createAck \cup {creating} /\ phase' = "idle"
                     /\ UNCHANGED <<catalog, stored, creating, createUnknown, observed, writeAuthorized, writeLease, dedupSeen>>
LoseCreateAck == /\ phase = "committed" /\ createUnknown' = createUnknown \cup {creating} /\ phase' = "idle"
                 /\ UNCHANGED <<catalog, stored, creating, createAck, observed, writeAuthorized, writeLease, dedupSeen>>
ReadSession(a, s) == /\ observed' = [ok |-> s \in catalog /\ Admitted(a, s),
                                    authorized |-> Authorized(a, s), content |-> TRUE, bound |-> TRUE, artifact |-> FALSE]
                     /\ UNCHANGED <<catalog, stored, phase, creating, createAck, createUnknown, writeAuthorized, writeLease, dedupSeen>>
PutArtifact(a, s, b, lease) ==
    /\ s \in catalog /\ Admitted(a, s) /\ (~CheckLease \/ lease)
    /\ LET key == <<s, b>> IN
       /\ (~CheckContent \/ stored[key] # "corrupt")
       /\ stored' = IF stored[key] = "absent" THEN [stored EXCEPT ![key] = "good"] ELSE stored
       /\ dedupSeen' = (dedupSeen \/ stored[key] = "good")
    /\ writeAuthorized' = Authorized(a, s) /\ writeLease' = lease
    /\ UNCHANGED <<catalog, phase, creating, createAck, createUnknown, observed>>
ReadArtifact(a, s, b, source) ==
    /\ source \in Sessions /\ (CheckSessionKey => source = s)
    /\ LET value == stored[<<source, b>>] IN
       observed' = [ok |-> s \in catalog /\ Admitted(a, s) /\ value # "absent" /\ (~CheckContent \/ value = "good"),
                    authorized |-> Authorized(a, s), content |-> value = "good",
                    bound |-> stored[<<s, b>>] # "absent", artifact |-> TRUE]
    /\ UNCHANGED <<catalog, stored, phase, creating, createAck, createUnknown, writeAuthorized, writeLease, dedupSeen>>
CorruptArtifact(key) == /\ stored[key] = "good" /\ stored' = [stored EXCEPT ![key] = "corrupt"]
                        /\ UNCHANGED <<catalog, phase, creating, createAck, createUnknown, observed, writeAuthorized, writeLease, dedupSeen>>
Next == \/ \E s \in Sessions : StartCreate(s)
        \/ AbortCreate \/ CommitCreate \/ AcknowledgeCreate \/ LoseCreateAck
        \/ \E a \in Actors, s \in Sessions : ReadSession(a, s)
        \/ \E a \in Actors, s \in Sessions, b \in Bodies, lease \in BOOLEAN : PutArtifact(a, s, b, lease)
        \/ \E a \in Actors, s \in Sessions, b \in Bodies, source \in Sessions : ReadArtifact(a, s, b, source)
        \/ \E key \in Keys : CorruptArtifact(key)
Spec == Init /\ [][Next]_vars
TypeOK == /\ catalog \subseteq Sessions /\ stored \in [Keys -> {"absent", "good", "corrupt"}]
          /\ phase \in {"idle", "staged", "committed"} /\ creating \in Sessions
          /\ createAck \subseteq Sessions /\ createUnknown \subseteq Sessions
          /\ observed \in [ok : BOOLEAN, authorized : BOOLEAN, content : BOOLEAN, bound : BOOLEAN, artifact : BOOLEAN]
          /\ writeAuthorized \in BOOLEAN /\ writeLease \in BOOLEAN /\ dedupSeen \in BOOLEAN
CreateAcknowledgedDurable == createAck \subseteq catalog
ArtifactHasSession == \A key \in Keys : stored[key] # "absent" => key[1] \in catalog
OwnerIsolation == (observed.ok => observed.authorized) /\ writeAuthorized
ArtifactContentChecked == observed.ok => observed.content
ArtifactSessionBound == observed.ok => observed.bound
ArtifactWriterLease == writeLease
NoCreateUnknownWitness == ~(phase = "idle" /\ createUnknown # {} /\ createUnknown \subseteq catalog)
NoArtifactDedupWitness == ~dedupSeen
NoAuthorizedReadWitness == ~(observed.ok /\ observed.artifact /\ observed.authorized /\ observed.content /\ observed.bound /\ catalog # {})
====
