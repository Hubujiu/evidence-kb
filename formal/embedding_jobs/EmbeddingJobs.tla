--------------------------- MODULE EmbeddingJobs ---------------------------
EXTENDS Naturals, FiniteSets, TLC

(* Durable job and physical execution have different lifetimes. Cancellation
   or lease expiration never establishes that execution was unsent. *)
CONSTANTS IgnoreFinishFence, RetryUnknown, TrustUnverifiedRecovery, SplitCommit
IDs == 1..2
Outcomes == {"none", "complete", "http_error", "unsent"}
JobStates == {"ABSENT", "PENDING", "RUNNING", "COMPLETE", "RETRY", "ERROR", "UNKNOWN"}
AttemptStates == {"NONE", "RUNNING", "COMPLETE", "ERROR", "UNKNOWN"}
NoTx == [op |-> "none", id |-> 0, kind |-> "none", recover |-> FALSE]
VARIABLES job, request, nextId, attempts, attemptCount, dependency, frozen,
          leaseLive, physical, sent, active, cancelled, tx,
          replaySafe, settlementSafe, recoverySafe
vars == <<job, request, nextId, attempts, attemptCount, dependency, frozen,
          leaseLive, physical, sent, active, cancelled, tx,
          replaySafe, settlementSafe, recoverySafe>>

Init ==
    /\ job = "ABSENT" /\ request = 0 /\ nextId = 1
    /\ attempts = [i \in IDs |-> "NONE"] /\ attemptCount = 0
    /\ dependency = 0 /\ frozen = 0 /\ leaseLive = FALSE
    /\ physical = [i \in IDs |-> "none"]
    /\ sent = [i \in IDs |-> FALSE]
    /\ active = [i \in IDs |-> FALSE]
    /\ cancelled = [i \in IDs |-> FALSE]
    /\ tx = NoTx
    /\ replaySafe = TRUE /\ settlementSafe = TRUE /\ recoverySafe = TRUE

Prepare(d) ==
    /\ job = "ABSENT" /\ tx.op = "none"
    /\ job' = "PENDING" /\ dependency' = d /\ frozen' = d
    /\ UNCHANGED <<request, nextId, attempts, attemptCount, leaseLive, physical, sent,
                   active, cancelled, tx, replaySafe, settlementSafe, recoverySafe>>

BeginClaim(d) ==
    /\ tx.op = "none" /\ d = dependency /\ nextId \in IDs
    /\ (job \in {"PENDING", "RETRY"} \/ (RetryUnknown /\ job = "UNKNOWN"))
    /\ tx' = [op |-> "claim", id |-> nextId, kind |-> "none", recover |-> FALSE]
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   leaseLive, physical, sent, active, cancelled,
                   replaySafe, settlementSafe, recoverySafe>>

CommitClaim ==
    /\ tx.op = "claim"
    /\ job' = "RUNNING" /\ request' = tx.id /\ nextId' = nextId + 1
    /\ attempts' = IF SplitCommit THEN attempts ELSE [attempts EXCEPT ![tx.id] = "RUNNING"]
    /\ attemptCount' = attemptCount + 1
    /\ leaseLive' = TRUE
    /\ replaySafe' = (replaySafe /\ (IF request = 0 THEN TRUE ELSE physical[request] \in {"unsent", "http_error"}))
    /\ tx' = [tx EXCEPT !.op = "ack"]
    /\ UNCHANGED <<dependency, frozen, physical, sent, active, cancelled, settlementSafe, recoverySafe>>

Send(i) ==
    /\ active[i] /\ ~sent[i] /\ physical[i] = "none"
    /\ sent' = [sent EXCEPT ![i] = TRUE]
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   leaseLive, physical, active, cancelled, tx,
                   replaySafe, settlementSafe, recoverySafe>>

Observe(i, outcome) ==
    /\ attempts[i] # "NONE" /\ physical[i] = "none"
    /\ IF outcome = "unsent" THEN ~sent[i] ELSE sent[i]
    /\ physical' = [physical EXCEPT ![i] = outcome]
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   leaseLive, sent, active, cancelled, tx,
                   replaySafe, settlementSafe, recoverySafe>>

Cancel(i) ==
    /\ active[i] /\ ~cancelled[i]
    /\ cancelled' = [cancelled EXCEPT ![i] = TRUE]
    /\ active' = [active EXCEPT ![i] = FALSE]
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   leaseLive, physical, sent, tx,
                   replaySafe, settlementSafe, recoverySafe>>

Elapse ==
    /\ request # 0 /\ leaseLive
    /\ leaseLive' = FALSE
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   physical, sent, active, cancelled, tx,
                   replaySafe, settlementSafe, recoverySafe>>

Expire ==
    /\ tx.op = "none" /\ job = "RUNNING" /\ ~leaseLive
    /\ job' = "UNKNOWN"
    /\ attempts' = [attempts EXCEPT ![request] = "UNKNOWN"]
    /\ UNCHANGED <<request, nextId, attemptCount, dependency, frozen, leaseLive, physical,
                   sent, active, cancelled, tx, replaySafe, settlementSafe, recoverySafe>>

Observed(i, kind, recover) ==
    IF kind = "ambiguous" THEN ~recover /\ physical[i] = "none"
    ELSE physical[i] = kind

BeginFinish(i, kind, recover) ==
    /\ tx.op = "none" /\ attempts[i] # "NONE"
    /\ (~recover \/ kind \in {"complete", "unsent"})
    /\ (Observed(i, kind, recover) \/ (TrustUnverifiedRecovery /\ recover /\ kind = "complete"))
    /\ tx' = [op |-> "finish", id |-> i, kind |-> kind, recover |-> recover]
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   leaseLive, physical, sent, active, cancelled,
                   replaySafe, settlementSafe, recoverySafe>>

StatusAndLeaseValid ==
    /\ IF tx.recover THEN job = "UNKNOWN"
       ELSE IF tx.kind = "unsent" THEN job \in {"RUNNING", "UNKNOWN"}
       ELSE job = "RUNNING"
    /\ (tx.recover \/ tx.kind = "unsent" \/ leaseLive)

ValidFinish == tx.id = request /\ StatusAndLeaseValid

FinishStatus ==
    CASE tx.kind = "complete" -> "COMPLETE"
      [] tx.kind = "unsent" -> "RETRY"
      [] tx.kind = "http_error" -> IF attemptCount < 2 THEN "RETRY" ELSE "ERROR"
      [] OTHER -> "UNKNOWN"

CommitFinish ==
    /\ tx.op = "finish" /\ StatusAndLeaseValid /\ (tx.id = request \/ IgnoreFinishFence)
    /\ job' = FinishStatus
    /\ attemptCount' = IF tx.kind = "unsent" /\ attemptCount > 0 THEN attemptCount - 1 ELSE attemptCount
    /\ attempts' = IF SplitCommit THEN attempts ELSE [attempts EXCEPT ![tx.id] =
          IF FinishStatus = "RETRY" THEN "ERROR" ELSE FinishStatus]
    /\ active' = [active EXCEPT ![tx.id] = FALSE]
    /\ settlementSafe' = (settlementSafe /\ ValidFinish)
    /\ recoverySafe' = (recoverySafe /\ (~tx.recover \/ Observed(tx.id, tx.kind, TRUE)))
    /\ tx' = [tx EXCEPT !.op = "ack"]
    /\ UNCHANGED <<request, nextId, dependency, frozen, leaseLive,
                   physical, sent, cancelled, replaySafe>>

(* Rollback also abstracts refusal after SQL request/status/lease predicates fail.
   An acknowledgment may be lost after commit; restart preserves durable rows. *)
FinishTransaction ==
    /\ tx.op # "none" /\ tx' = NoTx
    /\ active' = IF tx.op = "ack" /\ tx.kind = "none"
                  THEN [active EXCEPT ![tx.id] = TRUE] ELSE active
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   leaseLive, physical, sent, cancelled,
                   replaySafe, settlementSafe, recoverySafe>>

Restart ==
    /\ (tx.op # "none" \/ \E i \in IDs : active[i])
    /\ tx' = NoTx /\ active' = [i \in IDs |-> FALSE]
    /\ UNCHANGED <<job, request, nextId, attempts, attemptCount, dependency, frozen,
                   leaseLive, physical, sent, cancelled,
                   replaySafe, settlementSafe, recoverySafe>>

Next ==
    \/ \E d \in 1..2 : Prepare(d) \/ BeginClaim(d)
    \/ CommitClaim \/ CommitFinish \/ FinishTransaction \/ Restart \/ Elapse \/ Expire
    \/ \E i \in IDs : Send(i) \/ Cancel(i)
    \/ \E i \in IDs, outcome \in Outcomes \ {"none"} : Observe(i, outcome)
    \/ \E i \in IDs, kind \in {"complete", "unsent", "http_error", "ambiguous"}, recover \in BOOLEAN :
           BeginFinish(i, kind, recover)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ job \in JobStates /\ request \in 0..2 /\ nextId \in 1..3
    /\ attempts \in [IDs -> AttemptStates] /\ attemptCount \in 0..2
    /\ dependency \in 0..2 /\ frozen \in 0..2 /\ leaseLive \in BOOLEAN
    /\ physical \in [IDs -> Outcomes]
    /\ sent \in [IDs -> BOOLEAN] /\ active \in [IDs -> BOOLEAN] /\ cancelled \in [IDs -> BOOLEAN]
    /\ tx \in [op : {"none", "claim", "finish", "ack"}, id : 0..2,
               kind : {"none", "complete", "unsent", "http_error", "ambiguous"}, recover : BOOLEAN]
    /\ replaySafe \in BOOLEAN /\ settlementSafe \in BOOLEAN /\ recoverySafe \in BOOLEAN

FrozenDependency == dependency = frozen
NoBlindReplay == replaySafe
SettlementCorrelated == settlementSafe
RecoveryHasEvidence == recoverySafe
AtomicJobAttempt ==
    IF request = 0 THEN \A i \in IDs : attempts[i] = "NONE"
    ELSE attempts[request] = IF job = "RETRY" THEN "ERROR" ELSE job
CompletionBound == job = "COMPLETE" => request \in IDs /\ physical[request] = "complete"
=============================================================================
