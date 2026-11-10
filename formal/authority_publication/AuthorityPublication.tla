------------------------ MODULE AuthorityPublication -------------------------
EXTENDS Naturals, TLC

CONSTANTS SkipPostflight, SkipPublicationFence
Builds == {1, 2}
VARIABLES revision, active, phase, snapshot, expected, stored,
          readPhase, readRevision, readBuild, publicationSafe, responseSafe
vars == <<revision, active, phase, snapshot, expected, stored,
          readPhase, readRevision, readBuild, publicationSafe, responseSafe>>

Init ==
    /\ revision = 0 /\ active = 0
    /\ phase = [b \in Builds |-> "absent"]
    /\ snapshot = [b \in Builds |-> 0]
    /\ expected = [b \in Builds |-> 0]
    /\ stored = [b \in Builds |-> FALSE]
    /\ readPhase = "idle" /\ readRevision = 0 /\ readBuild = 0
    /\ publicationSafe = TRUE /\ responseSafe = TRUE

Stage(b) ==
    /\ phase[b] = "absent"
    /\ phase' = [phase EXCEPT ![b] = "staging"]
    /\ snapshot' = [snapshot EXCEPT ![b] = revision]
    /\ expected' = [expected EXCEPT ![b] = active]
    /\ UNCHANGED <<revision, active, stored, readPhase, readRevision, readBuild,
                   publicationSafe, responseSafe>>

Store(b) ==
    /\ phase[b] = "staging" /\ ~stored[b]
    /\ stored' = [stored EXCEPT ![b] = TRUE]
    /\ UNCHANGED <<revision, active, phase, snapshot, expected, readPhase,
                   readRevision, readBuild, publicationSafe, responseSafe>>

Validate(b) ==
    /\ phase[b] = "staging" /\ stored[b]
    \* Legacy non-resumable staging can validate a stale build; publish fences it.
    /\ phase' = [phase EXCEPT ![b] = "validated"]
    /\ UNCHANGED <<revision, active, snapshot, expected, stored, readPhase,
                   readRevision, readBuild, publicationSafe, responseSafe>>

Publish(b) ==
    /\ phase[b] = "validated"
    /\ SkipPublicationFence \/ (snapshot[b] = revision /\ expected[b] = active)
    \* Relational pointer and build status commit in one authority transaction.
    /\ active' = b /\ phase' = [phase EXCEPT ![b] = "published"]
    /\ publicationSafe' = (publicationSafe /\ stored[b]
                            /\ snapshot[b] = revision /\ expected[b] = active)
    /\ UNCHANGED <<revision, snapshot, expected, stored, readPhase,
                   readRevision, readBuild, responseSafe>>

Revoke ==
    /\ revision < 2 /\ revision' = revision + 1
    \* Published content remains stored; later readers must check current ACL.
    /\ UNCHANGED <<active, phase, snapshot, expected, stored, readPhase,
                   readRevision, readBuild, publicationSafe, responseSafe>>

BeginRead ==
    /\ readPhase = "idle" /\ active # 0
    /\ readPhase' = "authorized" /\ readRevision' = revision /\ readBuild' = active
    /\ UNCHANGED <<revision, active, phase, snapshot, expected, stored,
                   publicationSafe, responseSafe>>

LoadManifest ==
    /\ readPhase = "authorized" /\ readPhase' = "loaded"
    \* Separate backend/filesystem/dependency reads; not an authority transaction.
    /\ UNCHANGED <<revision, active, phase, snapshot, expected, stored,
                   readRevision, readBuild, publicationSafe, responseSafe>>

ReturnManifest ==
    /\ readPhase = "loaded"
    /\ SkipPostflight \/ (readRevision = revision /\ readBuild = active)
    /\ readPhase' = "returned"
    /\ responseSafe' = (responseSafe /\ readRevision = revision /\ readBuild = active)
    /\ UNCHANGED <<revision, active, phase, snapshot, expected, stored,
                   readRevision, readBuild, publicationSafe>>

RejectChangedRead ==
    /\ readPhase = "loaded" /\ (readRevision # revision \/ readBuild # active)
    /\ readPhase' = "rejected"
    /\ UNCHANGED <<revision, active, phase, snapshot, expected, stored,
                   readRevision, readBuild, publicationSafe, responseSafe>>

Next == (\E b \in Builds : Stage(b) \/ Store(b) \/ Validate(b) \/ Publish(b))
        \/ Revoke \/ BeginRead \/ LoadManifest \/ ReturnManifest \/ RejectChangedRead
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ revision \in 0..2 /\ active \in {0, 1, 2}
    /\ phase \in [Builds -> {"absent", "staging", "validated", "published"}]
    /\ snapshot \in [Builds -> 0..2] /\ expected \in [Builds -> {0, 1, 2}]
    /\ stored \in [Builds -> BOOLEAN]
    /\ readPhase \in {"idle", "authorized", "loaded", "returned", "rejected"}
    /\ readRevision \in 0..2 /\ readBuild \in {0, 1, 2}
    /\ publicationSafe \in BOOLEAN /\ responseSafe \in BOOLEAN
NoStagingVisible == active = 0 \/ (phase[active] = "published" /\ stored[active])
PublicationFenced == publicationSafe
ReadAuthorityCurrent == responseSafe
=============================================================================
