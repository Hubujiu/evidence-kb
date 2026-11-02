-------------------------- MODULE ConsoleUploadCommit -------------------------
EXTENDS Naturals, Sequences, TLC

CONSTANT GuardFinalCommit
NoOwner == [viewRevision |-> 0, kb |-> "none", token |-> "token"]
Scopes == {"old", "next"}
StampType == [viewRevision : 0..1, kb : Scopes, token : {"token"}]
Owners == [viewRevision : 0..1, kb : Scopes \cup {"none"}, token : {"token"}]
UploadPhases == {"idle", "fetch-await", "body-await", "body-complete",
                 "commit-queued", "complete", "discarded", "failed"}
CreatePhases == {"idle", "create-await", "refresh-await", "refresh-body-complete",
                 "select-queued", "clear-queued", "complete", "discarded"}
Jobs == {"upload-inner", "upload-commit", "refresh-inner", "refresh-select", "create-clear"}

VARIABLES scope, revision, upload, uploadOwner, create, createOwner,
          ingest, status, lastCommit, passedInner, microtasks
vars == <<scope, revision, upload, uploadOwner, create, createOwner,
          ingest, status, lastCommit, passedInner, microtasks>>
Stamp == [viewRevision |-> revision, kb |-> scope, token |-> "token"]
EmptyCommit == [wrote |-> FALSE, owner |-> NoOwner,
                atScope |-> [viewRevision |-> 0, kb |-> "old", token |-> "token"]]
HasJob(job) == Len(microtasks) > 0 /\ Head(microtasks) = job

Init ==
    /\ scope = "old" /\ revision = 0
    /\ upload = "idle" /\ uploadOwner = NoOwner
    /\ create = "idle" /\ createOwner = NoOwner
    /\ ingest = NoOwner /\ status = "empty"
    /\ lastCommit = EmptyCommit /\ passedInner = FALSE /\ microtasks = <<>>

StartUpload ==
    /\ microtasks = <<>> /\ upload = "idle" /\ scope = "old" /\ revision = 0
    /\ upload' = "fetch-await" /\ uploadOwner' = Stamp
    /\ UNCHANGED <<scope, revision, create, createOwner, ingest, status,
                   lastCommit, passedInner, microtasks>>

UploadHeaders ==
    /\ microtasks = <<>> /\ upload = "fetch-await"
    /\ upload' = IF uploadOwner = Stamp THEN "body-await" ELSE "discarded"
    /\ UNCHANGED <<scope, revision, uploadOwner, create, createOwner, ingest,
                   status, lastCommit, passedInner, microtasks>>

UploadBody ==
    /\ microtasks = <<>> /\ upload = "body-await"
    /\ upload' = "body-complete" /\ microtasks' = <<"upload-inner">>
    /\ UNCHANGED <<scope, revision, uploadOwner, create, createOwner, ingest,
                   status, lastCommit, passedInner>>

UploadInnerCheck ==
    /\ HasJob("upload-inner") /\ upload = "body-complete"
    /\ LET accepted == uploadOwner = Stamp
       IN /\ upload' = IF accepted THEN "commit-queued" ELSE "discarded"
          /\ passedInner' = accepted
          /\ microtasks' = IF accepted THEN Append(Tail(microtasks), "upload-commit")
                            ELSE Tail(microtasks)
    /\ UNCHANGED <<scope, revision, uploadOwner, create, createOwner, ingest,
                   status, lastCommit>>

UploadCommit ==
    /\ HasJob("upload-commit") /\ upload = "commit-queued"
    /\ LET accepted == ~GuardFinalCommit \/ uploadOwner = Stamp
       IN /\ upload' = IF accepted THEN "complete" ELSE "discarded"
          /\ ingest' = IF accepted THEN uploadOwner ELSE ingest
          /\ status' = IF accepted THEN "upload" ELSE status
          /\ lastCommit' = IF accepted
                THEN [wrote |-> TRUE, owner |-> uploadOwner, atScope |-> Stamp]
                ELSE lastCommit
    /\ microtasks' = Tail(microtasks)
    /\ UNCHANGED <<scope, revision, uploadOwner, create, createOwner, passedInner>>

RejectUpload ==
    /\ microtasks = <<>> /\ upload \in {"fetch-await", "body-await"}
    /\ upload' = "failed"
    /\ UNCHANGED <<scope, revision, uploadOwner, create, createOwner, ingest,
                   status, lastCommit, passedInner, microtasks>>

StartCreate ==
    /\ microtasks = <<>> /\ create = "idle" /\ scope = "old" /\ revision = 0
    /\ create' = "create-await" /\ createOwner' = Stamp
    /\ UNCHANGED <<scope, revision, upload, uploadOwner, ingest, status,
                   lastCommit, passedInner, microtasks>>

CreateResponse ==
    /\ microtasks = <<>> /\ create = "create-await"
    /\ create' = "refresh-await"
    /\ UNCHANGED <<scope, revision, upload, uploadOwner, createOwner, ingest,
                   status, lastCommit, passedInner, microtasks>>

RefreshBody ==
    /\ microtasks = <<>> /\ create = "refresh-await"
    /\ create' = "refresh-body-complete" /\ microtasks' = <<"refresh-inner">>
    /\ UNCHANGED <<scope, revision, upload, uploadOwner, createOwner, ingest,
                   status, lastCommit, passedInner>>

BothBodies(order) ==
    /\ microtasks = <<>> /\ upload = "body-await" /\ create = "refresh-await"
    \* A single response-delivery task fulfills both already pending bodies.
    /\ upload' = "body-complete" /\ create' = "refresh-body-complete"
    /\ microtasks' = order
    /\ UNCHANGED <<scope, revision, uploadOwner, createOwner, ingest,
                   status, lastCommit, passedInner>>

RefreshInnerCheck ==
    /\ HasJob("refresh-inner") /\ create = "refresh-body-complete"
    /\ LET accepted == createOwner = Stamp
       IN /\ create' = IF accepted THEN "select-queued" ELSE "discarded"
          /\ microtasks' = IF accepted THEN Append(Tail(microtasks), "refresh-select")
                            ELSE Tail(microtasks)
    /\ UNCHANGED <<scope, revision, upload, uploadOwner, createOwner, ingest,
                   status, lastCommit, passedInner>>

RefreshSelect ==
    /\ HasJob("refresh-select") /\ create = "select-queued"
    /\ scope' = "next" /\ create' = "clear-queued"
    /\ microtasks' = Append(Tail(microtasks), "create-clear")
    /\ UNCHANGED <<revision, upload, uploadOwner, createOwner, ingest,
                   status, lastCommit, passedInner>>

CreateClear ==
    /\ HasJob("create-clear") /\ create = "clear-queued"
    /\ revision' = 1 /\ create' = "complete" /\ status' = "created"
    /\ microtasks' = Tail(microtasks)
    \* clearView clears search/citation state, NOT the ingest field.
    /\ UNCHANGED <<scope, upload, uploadOwner, createOwner, ingest,
                   lastCommit, passedInner>>

Next == StartUpload \/ UploadHeaders \/ UploadBody \/ UploadInnerCheck \/
        UploadCommit \/ RejectUpload \/ StartCreate \/ CreateResponse \/
        RefreshBody \/ RefreshInnerCheck \/ RefreshSelect \/ CreateClear \/
        BothBodies(<<"refresh-inner", "upload-inner">>) \/
        BothBodies(<<"upload-inner", "refresh-inner">>)
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ scope \in Scopes /\ revision \in 0..1
    /\ upload \in UploadPhases /\ uploadOwner \in Owners
    /\ create \in CreatePhases /\ createOwner \in Owners
    /\ ingest \in Owners /\ status \in {"empty", "upload", "created"}
    /\ passedInner \in BOOLEAN /\ microtasks \in Seq(Jobs) /\ Len(microtasks) <= 2
    /\ lastCommit \in [wrote : BOOLEAN, owner : Owners, atScope : StampType]
NoStaleUploadCommit == lastCommit.wrote => lastCommit.owner = lastCommit.atScope
CurrentUploadRenders == upload = "complete" =>
    (lastCommit.wrote /\ ingest = uploadOwner)
NoRejectedUploadWrite == upload \in {"failed", "discarded"} => ~lastCommit.wrote
=============================================================================
