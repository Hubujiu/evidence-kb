------------------------- MODULE EmbeddingSourceView --------------------------
EXTENDS Naturals, TLC

CONSTANTS QueryUsesCandidate, FrozenPolicy
Versions == {1, 2}
Tasks == {"document", "query"}
VARIABLES candidate, published, staged, stagePhase, stageRevision, revision,
          permitted, requestPhase, task, auditVersion, auditBuild, auditRevision,
          decisionCorrect, permissionSafe
vars == <<candidate, published, staged, stagePhase, stageRevision, revision,
          permitted, requestPhase, task, auditVersion, auditBuild, auditRevision,
          decisionCorrect, permissionSafe>>

Init ==
    /\ candidate = 1 /\ published = 1 /\ staged = 0
    /\ stagePhase = "absent" /\ stageRevision = 0 /\ revision = 0
    /\ permitted = TRUE /\ requestPhase = "idle" /\ task = "query"
    /\ auditVersion = 1 /\ auditBuild = 1 /\ auditRevision = 0
    /\ decisionCorrect = TRUE /\ permissionSafe = TRUE

UploadCandidate ==
    /\ candidate = 1 /\ revision < 2
    /\ candidate' = 2 /\ revision' = revision + 1
    /\ UNCHANGED <<published, staged, stagePhase, stageRevision, permitted,
                   requestPhase, task, auditVersion, auditBuild, auditRevision,
                   decisionCorrect, permissionSafe>>

Stage ==
    /\ stagePhase = "absent" /\ permitted
    /\ staged' = candidate /\ stagePhase' = "staging" /\ stageRevision' = revision
    /\ UNCHANGED <<candidate, published, revision, permitted, requestPhase, task,
                   auditVersion, auditBuild, auditRevision, decisionCorrect, permissionSafe>>

Publish ==
    /\ stagePhase = "staging" /\ stageRevision = revision /\ staged = candidate
    /\ permitted
    /\ published' = staged /\ stagePhase' = "published"
    /\ UNCHANGED <<candidate, staged, stageRevision, revision, permitted,
                   requestPhase, task, auditVersion, auditBuild, auditRevision,
                   decisionCorrect, permissionSafe>>

Revoke ==
    /\ permitted /\ revision < 2
    /\ permitted' = FALSE /\ revision' = revision + 1
    /\ UNCHANGED <<candidate, published, staged, stagePhase, stageRevision,
                   requestPhase, task, auditVersion, auditBuild, auditRevision,
                   decisionCorrect, permissionSafe>>

BeginRequest(selectedTask) ==
    /\ requestPhase = "idle"
    /\ selectedTask = "query" \/ stagePhase = "staging"
    /\ requestPhase' = "pending" /\ task' = selectedTask
    /\ auditVersion' = IF selectedTask = "query" THEN published ELSE staged
    /\ auditBuild' = IF selectedTask = "query" THEN published ELSE staged
    /\ auditRevision' = revision
    /\ UNCHANGED <<candidate, published, staged, stagePhase, stageRevision, revision,
                   permitted, decisionCorrect, permissionSafe>>

BuildCurrent ==
    IF task = "query" THEN auditBuild = published
    ELSE stagePhase = "staging" /\ auditBuild = staged
         /\ staged = candidate /\ stageRevision = revision

Admit ==
    /\ requestPhase = "pending"
    /\ LET selected == IF task = "query" /\ ~QueryUsesCandidate THEN published ELSE candidate
           expected == IF task = "query" THEN published ELSE candidate
           current == auditRevision = revision /\ BuildCurrent
           accepted == current /\ (IF FrozenPolicy THEN TRUE ELSE permitted) /\ auditVersion = selected
           required == current /\ permitted /\ auditVersion = expected
       IN /\ requestPhase' = IF accepted THEN "admitted" ELSE "rejected"
          /\ decisionCorrect' = (decisionCorrect /\ (accepted = required))
          /\ permissionSafe' = (permissionSafe /\ (~accepted \/ permitted))
    /\ UNCHANGED <<candidate, published, staged, stagePhase, stageRevision, revision,
                   permitted, task, auditVersion, auditBuild, auditRevision>>

Next == UploadCandidate \/ Stage \/ Publish \/ Revoke \/ Admit
        \/ (\E selectedTask \in Tasks : BeginRequest(selectedTask))
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ candidate \in Versions /\ published \in Versions /\ staged \in {0, 1, 2}
    /\ stagePhase \in {"absent", "staging", "published"}
    /\ stageRevision \in 0..2 /\ revision \in 0..2 /\ permitted \in BOOLEAN
    /\ requestPhase \in {"idle", "pending", "admitted", "rejected"}
    /\ task \in Tasks /\ auditVersion \in Versions /\ auditBuild \in Versions
    /\ auditRevision \in 0..2 /\ decisionCorrect \in BOOLEAN /\ permissionSafe \in BOOLEAN
TaskSpecificSourceView == decisionCorrect
AdmissionUsesLivePolicy == permissionSafe
PublishedQueryWitness == ~(requestPhase = "admitted" /\ task = "query" /\ candidate = 2 /\ published = 1)
CandidateBuildWitness == ~(requestPhase = "admitted" /\ task = "document" /\ candidate = 2 /\ published = 1)
=============================================================================
