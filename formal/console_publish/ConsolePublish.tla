---------------------------- MODULE ConsolePublish ----------------------------
EXTENDS Naturals, Sequences, TLC

CONSTANTS GuardBeforeSend, GuardFinalCommit
Scopes == {"old", "next"}
StampType == [revision : 0..1, scope : Scopes]
EmptyEffect == [occurred |-> FALSE, owner |-> [revision |-> 0, scope |-> "old"],
                atScope |-> [revision |-> 0, scope |-> "old"]]
Phases == {"idle", "build-await", "build-body", "send-queued", "publish-await",
           "publish-body", "commit-queued", "complete", "discarded", "failed"}
CreatePhases == {"idle", "refresh-await", "refresh-body", "select-queued",
                 "clear-queued", "complete"}
Jobs == {"build-inner", "publish-send", "publish-inner", "publish-commit",
         "refresh-inner", "refresh-select", "create-clear"}

VARIABLES scope, revision, phase, owner, create, jobs, dispatch, commit, status
vars == <<scope, revision, phase, owner, create, jobs, dispatch, commit, status>>
Stamp == [revision |-> revision, scope |-> scope]
HasJob(job) == Len(jobs) > 0 /\ Head(jobs) = job

Init ==
    /\ scope = "old" /\ revision = 0 /\ phase = "idle" /\ owner = Stamp
    /\ create = "idle" /\ jobs = <<>>
    /\ dispatch = EmptyEffect /\ commit = EmptyEffect /\ status = "empty"

StartPublish ==
    /\ jobs = <<>> /\ phase = "idle" /\ scope = "old" /\ revision = 0
    /\ phase' = "build-await" /\ owner' = Stamp
    /\ UNCHANGED <<scope, revision, create, jobs, dispatch, commit, status>>

Body(kind) ==
    /\ jobs = <<>>
    /\ phase = IF kind = "build" THEN "build-await" ELSE "publish-await"
    /\ phase' = IF kind = "build" THEN "build-body" ELSE "publish-body"
    /\ jobs' = <<IF kind = "build" THEN "build-inner" ELSE "publish-inner">>
    /\ UNCHANGED <<scope, revision, owner, create, dispatch, commit, status>>

Inner(kind) ==
    /\ HasJob(IF kind = "build" THEN "build-inner" ELSE "publish-inner")
    /\ phase = IF kind = "build" THEN "build-body" ELSE "publish-body"
    /\ LET accepted == owner = Stamp
           next == IF kind = "build" THEN "publish-send" ELSE "publish-commit"
       IN /\ phase' = IF accepted
                     THEN IF kind = "build" THEN "send-queued" ELSE "commit-queued"
                     ELSE "discarded"
          /\ jobs' = IF accepted THEN Append(Tail(jobs), next) ELSE Tail(jobs)
    /\ UNCHANGED <<scope, revision, owner, create, dispatch, commit, status>>

Send ==
    /\ HasJob("publish-send") /\ phase = "send-queued"
    /\ LET accepted == ~GuardBeforeSend \/ owner = Stamp
       IN /\ phase' = IF accepted THEN "publish-await" ELSE "discarded"
          /\ dispatch' = IF accepted
               THEN [occurred |-> TRUE, owner |-> owner, atScope |-> Stamp]
               ELSE dispatch
    /\ jobs' = Tail(jobs)
    /\ UNCHANGED <<scope, revision, owner, create, commit, status>>

Commit ==
    /\ HasJob("publish-commit") /\ phase = "commit-queued"
    /\ LET accepted == ~GuardFinalCommit \/ owner = Stamp
       IN /\ phase' = IF accepted THEN "complete" ELSE "discarded"
          /\ commit' = IF accepted
               THEN [occurred |-> TRUE, owner |-> owner, atScope |-> Stamp]
               ELSE commit
          /\ status' = IF accepted THEN "published" ELSE status
    /\ jobs' = Tail(jobs)
    /\ UNCHANGED <<scope, revision, owner, create, dispatch>>

Reject ==
    /\ jobs = <<>> /\ phase \in {"build-await", "publish-await"}
    /\ phase' = "failed"
    /\ UNCHANGED <<scope, revision, owner, create, jobs, dispatch, commit, status>>

StartCreate ==
    /\ jobs = <<>> /\ create = "idle" /\ scope = "old" /\ revision = 0
    /\ create' = "refresh-await"
    /\ UNCHANGED <<scope, revision, phase, owner, jobs, dispatch, commit, status>>

RefreshBody ==
    /\ jobs = <<>> /\ create = "refresh-await"
    /\ create' = "refresh-body" /\ jobs' = <<"refresh-inner">>
    /\ UNCHANGED <<scope, revision, phase, owner, dispatch, commit, status>>

BothBodies(kind, order) ==
    /\ jobs = <<>> /\ create = "refresh-await"
    /\ phase = IF kind = "build" THEN "build-await" ELSE "publish-await"
    /\ phase' = IF kind = "build" THEN "build-body" ELSE "publish-body"
    /\ create' = "refresh-body" /\ jobs' = order
    /\ UNCHANGED <<scope, revision, owner, dispatch, commit, status>>

RefreshInner ==
    /\ HasJob("refresh-inner") /\ create = "refresh-body"
    /\ create' = "select-queued" /\ jobs' = Append(Tail(jobs), "refresh-select")
    /\ UNCHANGED <<scope, revision, phase, owner, dispatch, commit, status>>

Select ==
    /\ HasJob("refresh-select") /\ create = "select-queued"
    /\ scope' = "next" /\ create' = "clear-queued"
    /\ jobs' = Append(Tail(jobs), "create-clear")
    /\ UNCHANGED <<revision, phase, owner, dispatch, commit, status>>

Clear ==
    /\ HasJob("create-clear") /\ create = "clear-queued"
    /\ revision' = 1 /\ create' = "complete" /\ status' = "created"
    /\ jobs' = Tail(jobs)
    /\ UNCHANGED <<scope, phase, owner, dispatch, commit>>

Next == StartPublish \/ Body("build") \/ Body("publish") \/
        Inner("build") \/ Inner("publish") \/ Send \/ Commit \/ Reject \/
        StartCreate \/ RefreshBody \/ RefreshInner \/ Select \/ Clear \/
        BothBodies("build", <<"refresh-inner", "build-inner">>) \/
        BothBodies("build", <<"build-inner", "refresh-inner">>) \/
        BothBodies("publish", <<"refresh-inner", "publish-inner">>) \/
        BothBodies("publish", <<"publish-inner", "refresh-inner">>)
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ scope \in Scopes /\ revision \in 0..1 /\ phase \in Phases /\ owner \in StampType
    /\ create \in CreatePhases /\ jobs \in Seq(Jobs) /\ Len(jobs) <= 2
    /\ dispatch \in [occurred : BOOLEAN, owner : StampType, atScope : StampType]
    /\ commit \in [occurred : BOOLEAN, owner : StampType, atScope : StampType]
    /\ status \in {"empty", "published", "created"}
NoStalePublishSend == dispatch.occurred => dispatch.owner = dispatch.atScope
NoStalePublishCommit == commit.occurred => commit.owner = commit.atScope
CurrentPublishRenders == phase = "complete" => (dispatch.occurred /\ commit.occurred)
NoRejectedPublishWrite == phase \in {"failed", "discarded"} => ~commit.occurred
=============================================================================
