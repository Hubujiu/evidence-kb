------------------------- MODULE TransferOwnership ------------------------
EXTENDS Naturals, TLC
CONSTANT DiscardInflight
VARIABLES closed, mode, streamAlive, offset, hashValid, validated, submitted,
          job, handler, calls, actualEffects, largeResult, downloadAlive,
          readOffset, client
vars == <<closed, mode, streamAlive, offset, hashValid, validated, submitted,
          job, handler, calls, actualEffects, largeResult, downloadAlive,
          readOffset, client>>
Init == /\ closed = FALSE /\ mode = "upload" /\ streamAlive = TRUE /\ offset = 0
        /\ hashValid \in BOOLEAN /\ validated = FALSE /\ submitted = FALSE
        /\ job = "idle" /\ handler = "NotStarted" /\ calls = 0 /\ actualEffects = 0
        /\ largeResult \in BOOLEAN /\ downloadAlive = FALSE /\ readOffset = 0 /\ client = "None"
WriteChunk ==
    /\ ~closed /\ mode = "upload" /\ offset < 2 /\ ~submitted
    /\ offset' = offset + 1
    /\ UNCHANGED <<closed, mode, streamAlive, hashValid, validated, submitted, job,
                    handler, calls, actualEffects, largeResult, downloadAlive, readOffset, client>>
Submit ==
    /\ mode = "upload" /\ offset = 2 /\ ~submitted
    /\ submitted' = TRUE
    /\ UNCHANGED <<closed, mode, streamAlive, offset, hashValid, validated, job,
                    handler, calls, actualEffects, largeResult, downloadAlive, readOffset, client>>
SubmissionLost ==
    /\ submitted /\ job = "idle" /\ client = "None"
    /\ client' = "Unknown"
    /\ UNCHANGED <<closed, mode, streamAlive, offset, hashValid, validated, submitted,
                    job, handler, calls, actualEffects, largeResult, downloadAlive, readOffset>>
ClaimDispatch ==
    /\ ~closed /\ mode = "upload" /\ submitted /\ job = "idle" /\ client = "None"
    /\ mode' = "dispatch" /\ job' = "verifying"
    /\ UNCHANGED <<closed, streamAlive, offset, hashValid, validated, submitted,
                    handler, calls, actualEffects, largeResult, downloadAlive, readOffset, client>>
VerifyGood ==
    /\ job = "verifying" /\ hashValid /\ offset = 2
    /\ validated' = TRUE /\ job' = "ready"
    /\ UNCHANGED <<closed, mode, streamAlive, offset, hashValid, submitted,
                    handler, calls, actualEffects, largeResult, downloadAlive, readOffset, client>>
VerifyBad ==
    /\ job = "verifying" /\ ~hashValid
    /\ mode' = "absent" /\ streamAlive' = FALSE /\ job' = "done" /\ client' = "Rejected"
    /\ UNCHANGED <<closed, offset, hashValid, validated, submitted, handler,
                    calls, actualEffects, largeResult, downloadAlive, readOffset>>
Invoke ==
    /\ job = "ready" /\ handler = "NotStarted"
    /\ job' = "running" /\ handler' = "Running" /\ calls' = calls + 1
    /\ UNCHANGED <<closed, mode, streamAlive, offset, hashValid, validated, submitted,
                    actualEffects, largeResult, downloadAlive, readOffset, client>>
Return(ok, effects) ==
    /\ job = "running" /\ ok \in BOOLEAN /\ effects \in 0..1
    /\ handler' = IF ok THEN "Succeeded" ELSE "Errored"
    /\ actualEffects' = effects /\ job' = "returned"
    /\ UNCHANGED <<closed, mode, streamAlive, offset, hashValid, validated, submitted,
                    calls, largeResult, downloadAlive, readOffset, client>>
Publish(delivered, capacity) ==
    /\ job = "returned" /\ delivered \in BOOLEAN /\ capacity \in BOOLEAN
    /\ LET failed == largeResult /\ (closed \/ ~capacity)
           download == handler = "Succeeded" /\ largeResult /\ ~failed
       IN /\ downloadAlive' = download
          /\ client' = IF handler = "Errored" THEN "Unknown"
                        ELSE IF failed THEN "CompletedUnavailable"
                        ELSE IF ~delivered THEN "Unknown"
                        ELSE IF download THEN "Downloading" ELSE "Complete"
    /\ mode' = "absent" /\ streamAlive' = FALSE /\ job' = "done"
    /\ UNCHANGED <<closed, offset, hashValid, validated, submitted, handler,
                    calls, actualEffects, largeResult, readOffset>>
ReadChunk ==
    /\ ~closed /\ downloadAlive /\ client = "Downloading" /\ readOffset < 2
    /\ readOffset' = readOffset + 1
    /\ downloadAlive' = (readOffset' < 2)
    /\ client' = IF readOffset' = 2 THEN "Complete" ELSE "Downloading"
    /\ UNCHANGED <<closed, mode, streamAlive, offset, hashValid, validated, submitted,
                    job, handler, calls, actualEffects, largeResult>>
DropIdleTransfer ==
    /\ ~closed /\ (mode = "upload" \/ downloadAlive)
    /\ mode' = IF mode = "upload" THEN "absent" ELSE mode
    /\ streamAlive' = IF mode = "upload" THEN FALSE ELSE streamAlive
    /\ downloadAlive' = FALSE /\ client' = "Unknown"
    /\ UNCHANGED <<closed, offset, hashValid, validated, submitted, job, handler,
                    calls, actualEffects, largeResult, readOffset>>
Close ==
    /\ ~closed /\ closed' = TRUE
    /\ mode' = IF mode = "upload" THEN "absent" ELSE mode
    /\ streamAlive' = IF mode = "upload" \/ DiscardInflight THEN FALSE ELSE streamAlive
    /\ downloadAlive' = FALSE
    /\ UNCHANGED <<offset, hashValid, validated, submitted, job, handler, calls,
                    actualEffects, largeResult, readOffset, client>>
Next == \/ WriteChunk \/ Submit \/ SubmissionLost \/ ClaimDispatch \/ VerifyGood
        \/ VerifyBad \/ Invoke \/ ReadChunk \/ DropIdleTransfer \/ Close
        \/ \E ok \in BOOLEAN, effects \in 0..1: Return(ok, effects)
        \/ \E delivered \in BOOLEAN, capacity \in BOOLEAN: Publish(delivered, capacity)
Spec == Init /\ [][Next]_vars
TypeOK == /\ closed \in BOOLEAN /\ mode \in {"upload", "dispatch", "absent"}
          /\ streamAlive \in BOOLEAN /\ offset \in 0..2 /\ hashValid \in BOOLEAN
          /\ validated \in BOOLEAN /\ submitted \in BOOLEAN
          /\ job \in {"idle", "verifying", "ready", "running", "returned", "done"}
          /\ handler \in {"NotStarted", "Running", "Succeeded", "Errored"}
          /\ calls \in 0..1 /\ actualEffects \in 0..1 /\ largeResult \in BOOLEAN
          /\ downloadAlive \in BOOLEAN /\ readOffset \in 0..2
          /\ client \in {"None", "Unknown", "Rejected", "CompletedUnavailable", "Downloading", "Complete"}
DispatchOwnsStream == job \in {"verifying", "ready", "running", "returned"}
                     => mode = "dispatch" /\ streamAlive
HandlerRequiresIntegrity == calls > 0 => validated /\ hashValid /\ offset = 2
NoReplay == calls <= 1
ClosedOwnsNoIdleResources == closed => mode # "upload" /\ ~downloadAlive
KnownCompletionNeedsReturn == client \in {"Complete", "CompletedUnavailable", "Downloading"}
                             => handler = "Succeeded"
=============================================================================
