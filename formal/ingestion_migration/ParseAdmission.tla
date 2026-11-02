--------------------------- MODULE ParseAdmission ---------------------------
EXTENDS Naturals
CONSTANTS ReadyGuard, IdentityGuard, RevisionGuard
VARIABLES phase, outcome, originalStored, parsedIdentity, revision,
          capturedRevision, authorized, committedIdentity, built, hasEvidence
vars == <<phase, outcome, originalStored, parsedIdentity, revision,
          capturedRevision, authorized, committedIdentity, built, hasEvidence>>
Outcomes == {"READY", "PARTIAL", "BLOCKED", "FAILED"}
Init == /\ phase = "idle" /\ outcome = "none" /\ originalStored = FALSE
        /\ parsedIdentity = "none" /\ revision = 0 /\ capturedRevision = 0
        /\ authorized = TRUE /\ committedIdentity = "none" /\ built = FALSE /\ hasEvidence = FALSE
StoreOriginal == /\ phase = "idle" /\ authorized
                 /\ phase' = "stored" /\ originalStored' = TRUE
                 /\ capturedRevision' = revision
                 /\ UNCHANGED <<outcome, parsedIdentity, revision, authorized, committedIdentity, built, hasEvidence>>
Parse(result, identity, evidence) == /\ phase = "stored" /\ result \in Outcomes
                          /\ identity \in {"original", "wrong"} /\ evidence \in BOOLEAN
                          /\ (evidence => result \in {"READY", "PARTIAL"})
                          /\ phase' = "parsed" /\ outcome' = result /\ parsedIdentity' = identity
                          /\ hasEvidence' = evidence
                          /\ UNCHANGED <<originalStored, revision, capturedRevision, authorized,
                                         committedIdentity, built>>
ChangePolicy == /\ phase \in {"stored", "parsed"} /\ revision = 0
                /\ revision' = 1
                /\ UNCHANGED <<phase, outcome, originalStored, parsedIdentity, capturedRevision,
                               authorized, committedIdentity, built, hasEvidence>>
Revoke == /\ phase \in {"stored", "parsed"} /\ authorized
          /\ authorized' = FALSE
          /\ UNCHANGED <<phase, outcome, originalStored, parsedIdentity, revision,
                         capturedRevision, committedIdentity, built, hasEvidence>>
Admissible == authorized /\ (~IdentityGuard \/ parsedIdentity = "original")
             /\ (~RevisionGuard \/ revision = capturedRevision)
Commit == /\ phase = "parsed" /\ Admissible
          /\ phase' = "committed" /\ committedIdentity' = parsedIdentity
          /\ UNCHANGED <<outcome, originalStored, parsedIdentity, revision, capturedRevision, authorized, built, hasEvidence>>
Reject == /\ phase = "parsed" /\ ~Admissible /\ phase' = "rejected"
          /\ UNCHANGED <<outcome, originalStored, parsedIdentity, revision, capturedRevision,
                         authorized, committedIdentity, built, hasEvidence>>
Build == /\ phase = "committed" /\ ~built /\ authorized /\ hasEvidence
         /\ (~ReadyGuard \/ outcome = "READY") /\ built' = TRUE
         /\ UNCHANGED <<phase, outcome, originalStored, parsedIdentity, revision, capturedRevision,
                        authorized, committedIdentity, hasEvidence>>
Crash == /\ phase \in {"idle", "stored", "parsed"} /\ phase' = "crashed"
         /\ UNCHANGED <<outcome, originalStored, parsedIdentity, revision, capturedRevision,
                        authorized, committedIdentity, built, hasEvidence>>
Next == StoreOriginal \/ (\E result \in Outcomes, identity \in {"original", "wrong"}, evidence \in BOOLEAN : Parse(result, identity, evidence))
        \/ ChangePolicy \/ Revoke \/ Commit \/ Reject \/ Build \/ Crash
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"idle", "stored", "parsed", "committed", "rejected", "crashed"}
          /\ outcome \in Outcomes \cup {"none"} /\ originalStored \in BOOLEAN
          /\ parsedIdentity \in {"none", "original", "wrong"}
          /\ committedIdentity \in {"none", "original", "wrong"}
          /\ revision \in 0..1 /\ capturedRevision \in 0..1
          /\ authorized \in BOOLEAN /\ built \in BOOLEAN /\ hasEvidence \in BOOLEAN
NoIncompleteBuild == built => outcome = "READY"
NoEmptyBuild == built => hasEvidence
ImmutableOriginalIdentity == committedIdentity # "none" => originalStored /\ committedIdentity = "original"
CurrentPolicyAtCommit == phase = "committed" => revision = capturedRevision /\ authorized
SuccessUnreachable == ~built
=============================================================================
