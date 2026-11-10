------------------------ MODULE AtomicAuthorityCommit ------------------------
EXTENDS Naturals

CONSTANTS CheckAuthority, HoldLock
ASSUME /\ CheckAuthority \in BOOLEAN /\ HoldLock \in BOOLEAN

VARIABLES phase, revision, manifest, allowed, initialRevision, initialManifest,
          locked, committedRevision, committedManifest, committedAllowed,
          completions, acknowledgement
vars == <<phase, revision, manifest, allowed, initialRevision, initialManifest,
          locked, committedRevision, committedManifest, committedAllowed,
          completions, acknowledgement>>

Init ==
    /\ phase = "checked" /\ revision = 0 /\ manifest = 0 /\ allowed = TRUE
    /\ initialRevision = 0 /\ initialManifest = 0 /\ locked = FALSE
    /\ committedRevision = 0 /\ committedManifest = 0 /\ committedAllowed = FALSE
    /\ completions = 0 /\ acknowledgement = "none"

Acquire ==
    /\ phase = "checked"
    /\ LET valid == allowed /\ revision = initialRevision /\ manifest = initialManifest IN
       /\ phase' = IF ~CheckAuthority \/ valid THEN "staged" ELSE "rejected"
       /\ locked' = (~CheckAuthority \/ valid)
    /\ UNCHANGED <<revision, manifest, allowed, initialRevision, initialManifest,
          committedRevision, committedManifest, committedAllowed, completions, acknowledgement>>

Revoke ==
    /\ (~locked \/ ~HoldLock)
    /\ revision = 0 /\ revision' = 1 /\ allowed' = FALSE
    /\ UNCHANGED <<phase, manifest, initialRevision, initialManifest, locked,
          committedRevision, committedManifest, committedAllowed, completions, acknowledgement>>

Publish ==
    /\ (~locked \/ ~HoldLock)
    /\ manifest = 0 /\ manifest' = 1
    /\ UNCHANGED <<phase, revision, allowed, initialRevision, initialManifest, locked,
          committedRevision, committedManifest, committedAllowed, completions, acknowledgement>>

Commit(ack) ==
    /\ phase = "staged" /\ phase' = "durable" /\ locked' = FALSE
    /\ committedRevision' = revision /\ committedManifest' = manifest
    /\ committedAllowed' = allowed /\ completions' = completions + 1
    /\ acknowledgement' = ack
    /\ UNCHANGED <<revision, manifest, allowed, initialRevision, initialManifest>>

Rollback ==
    /\ phase = "staged" /\ phase' = "rolled_back" /\ locked' = FALSE
    /\ acknowledgement' = "unknown"
    /\ UNCHANGED <<revision, manifest, allowed, initialRevision, initialManifest,
          committedRevision, committedManifest, committedAllowed, completions>>

Resolve ==
    /\ phase = "durable" /\ acknowledgement = "unknown"
    /\ acknowledgement' = "resolved"
    /\ UNCHANGED <<phase, revision, manifest, allowed, initialRevision, initialManifest,
          locked, committedRevision, committedManifest, committedAllowed, completions>>

Next == Acquire \/ Revoke \/ Publish \/ Rollback \/ Resolve
        \/ Commit("received") \/ Commit("unknown")

TypeOK ==
    /\ phase \in {"checked", "staged", "rejected", "durable", "rolled_back"}
    /\ revision \in 0..1 /\ manifest \in 0..1
    /\ initialRevision = 0 /\ initialManifest = 0
    /\ locked \in BOOLEAN /\ allowed \in BOOLEAN /\ committedAllowed \in BOOLEAN
    /\ committedRevision \in 0..1 /\ committedManifest \in 0..1
    /\ completions \in 0..1
    /\ acknowledgement \in {"none", "received", "unknown", "resolved"}
CurrentAuthorityAtCommit == phase = "durable" =>
    (committedAllowed /\ committedRevision = initialRevision /\ committedManifest = initialManifest)
AtomicCompletion == (phase = "durable") <=> (completions = 1)
NoUnknownRetry == acknowledgement = "resolved" => completions = 1
NoDurableUnknownWitness == ~(phase = "durable" /\ acknowledgement = "unknown" /\ completions = 1)
NoRollbackWitness == ~(phase = "rolled_back" /\ completions = 0 /\ ~locked)

Spec == Init /\ [][Next]_vars
=============================================================================
