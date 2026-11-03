----------------------- MODULE EmbeddingLeaseBoundaries -----------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS IgnoreLease, IgnoreRecoveryRequest, IgnoreRecoveryState,
          IgnoreSchedule, RefundObservedFailure, ReconcileAtEquality,
          SkipCircuitFailure, SkipCircuitReset
IDs == 1..2
MaxClock == 5
Kinds == {"none", "complete", "http", "fatal", "ambiguous", "unsent", "preflight", "blocked"}
States == {"PENDING", "RUNNING", "UNKNOWN", "RETRY", "ERROR", "COMPLETE"}
VARIABLES now, job, attempts, nextId, physical, circuit, circuitExpected, recovery,
          charged, leaseSafe, recoverySafe, scheduleSafe, expirySafe, last
vars == <<now, job, attempts, nextId, physical, circuit, circuitExpected, recovery,
          charged, leaseSafe, recoverySafe, scheduleSafe, expirySafe, last>>

Init ==
    /\ now = 0
    /\ job = [status |-> "PENDING", id |-> 0, count |-> 0, until |-> 0, retry |-> 0]
    /\ attempts = [i \in IDs |-> "NONE"] /\ nextId = 1
    /\ physical = [i \in IDs |-> "none"]
    /\ circuit = [failures |-> 0, until |-> 0]
    /\ circuitExpected = [failures |-> 0, until |-> 0]
    /\ recovery = 0 /\ charged = {}
    /\ leaseSafe = TRUE /\ recoverySafe = TRUE
    /\ scheduleSafe = TRUE /\ expirySafe = TRUE /\ last = "init"

Tick ==
    /\ now < MaxClock /\ now' = now + 1
    /\ UNCHANGED <<job, attempts, nextId, physical, circuit, circuitExpected, recovery, charged,
                   leaseSafe, recoverySafe, scheduleSafe, expirySafe, last>>

Claim ==
    /\ job.status \in {"PENDING", "RETRY"} /\ nextId \in IDs
    /\ IgnoreSchedule \/ (job.retry <= now /\ circuit.until <= now)
    /\ job' = [status |-> "RUNNING", id |-> nextId, count |-> job.count + 1,
               until |-> now + 1, retry |-> 0]
    /\ attempts' = [attempts EXCEPT ![nextId] = "RUNNING"]
    /\ charged' = charged \cup {nextId} /\ nextId' = nextId + 1
    /\ scheduleSafe' = (scheduleSafe /\ job.retry <= now /\ circuit.until <= now)
    /\ last' = IF job.status = "RETRY" /\ job.retry > 0 /\ job.retry = now THEN "retry-equal" ELSE "claim"
    /\ UNCHANGED <<now, physical, circuit, circuitExpected, recovery, leaseSafe, recoverySafe, expirySafe>>

Expire(byClaim) ==
    /\ job.status = "RUNNING"
    /\ IF byClaim \/ ReconcileAtEquality THEN job.until <= now ELSE job.until < now
    /\ job' = [job EXCEPT !.status = "UNKNOWN"]
    /\ attempts' = [attempts EXCEPT ![job.id] = "UNKNOWN"]
    /\ expirySafe' = (expirySafe /\ (byClaim \/ job.until < now))
    /\ last' = IF byClaim /\ job.until = now THEN "claim-equal" ELSE "expired"
    /\ UNCHANGED <<now, nextId, physical, circuit, circuitExpected, recovery, charged,
                   leaseSafe, recoverySafe, scheduleSafe>>

Observe(i, kind) ==
    /\ i < nextId /\ physical[i] = "none" /\ kind \notin {"none", "ambiguous"}
    /\ physical' = [physical EXCEPT ![i] = kind]
    /\ UNCHANGED <<now, job, attempts, nextId, circuit, circuitExpected, recovery, charged,
                   leaseSafe, recoverySafe, scheduleSafe, expirySafe, last>>

SnapshotRecovery ==
    /\ job.status = "UNKNOWN" /\ recovery = 0
    /\ recovery' = job.id
    /\ UNCHANGED <<now, job, attempts, nextId, physical, circuit, circuitExpected, charged,
                   leaseSafe, recoverySafe, scheduleSafe, expirySafe, last>>

DropRecovery ==
    /\ recovery # 0 /\ recovery' = 0
    /\ UNCHANGED <<now, job, attempts, nextId, physical, circuit, circuitExpected, charged,
                   leaseSafe, recoverySafe, scheduleSafe, expirySafe, last>>

Unsent(kind) == kind \in {"unsent", "blocked"}
Result(kind) ==
    CASE kind = "complete" -> "COMPLETE"
      [] Unsent(kind) -> "RETRY"
      [] kind = "http" -> IF job.count < 2 THEN "RETRY" ELSE "ERROR"
      [] kind = "ambiguous" -> "UNKNOWN"
      [] OTHER -> "ERROR"

Settle(i, kind, recover) ==
    LET unsent == Unsent(kind)
        result == Result(kind)
        refund == unsent \/ (RefundObservedFailure /\ kind = "http")
    IN
    /\ i < nextId /\ kind # "none"
    /\ IF kind = "ambiguous" THEN physical[i] = "none" ELSE physical[i] = kind
    /\ IF recover
       THEN /\ recovery = i /\ kind \in {"complete", "unsent"}
            /\ (IgnoreRecoveryRequest \/ job.id = i)
            /\ (IgnoreRecoveryState \/ job.status = "UNKNOWN")
       ELSE /\ job.id = i
            /\ (job.status = "RUNNING" \/ (unsent /\ job.status = "UNKNOWN"))
            /\ (IgnoreLease \/ unsent \/ job.until >= now)
    /\ job' = [job EXCEPT !.status = result,
                         !.count = IF refund /\ @ > 0 THEN @ - 1 ELSE @,
                         !.retry = IF kind = "http" THEN now + 1 ELSE 0]
    /\ attempts' = [attempts EXCEPT ![i] = IF result = "RETRY" THEN "ERROR" ELSE result]
    /\ charged' = IF unsent THEN charged \ {i} ELSE charged
    /\ circuit' = IF kind = "complete" /\ ~SkipCircuitReset THEN [failures |-> 0, until |-> 0]
                   ELSE IF kind \in {"http", "fatal", "ambiguous"} /\ ~SkipCircuitFailure
                        THEN [failures |-> circuit.failures + 1, until |-> now + 1]
                        ELSE circuit
    /\ circuitExpected' = IF kind = "complete" THEN [failures |-> 0, until |-> 0]
                           ELSE IF kind \in {"http", "fatal", "ambiguous"}
                                THEN [failures |-> circuitExpected.failures + 1, until |-> now + 1]
                                ELSE circuitExpected
    /\ leaseSafe' = (leaseSafe /\ (recover \/ unsent \/ job.until >= now))
    /\ recoverySafe' = (recoverySafe /\ (~recover \/ (job.id = i /\ job.status = "UNKNOWN")))
    /\ last' = IF recover /\ kind = "complete" THEN "recovered-complete"
                ELSE IF recover /\ kind = "unsent" THEN "recovered-unsent"
                ELSE IF kind = "complete" /\ job.until = now THEN "finish-equal"
                ELSE "settled"
    /\ UNCHANGED <<now, nextId, physical, recovery, scheduleSafe, expirySafe>>

Next == Tick \/ Claim \/ (\E claim \in BOOLEAN : Expire(claim))
        \/ SnapshotRecovery \/ DropRecovery
        \/ (\E i \in IDs, kind \in Kinds : Observe(i, kind))
        \/ (\E i \in IDs, kind \in Kinds, recover \in BOOLEAN : Settle(i, kind, recover))
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ now \in 0..MaxClock /\ nextId \in 1..3
    /\ job \in [status : States, id : 0..2, count : 0..2, until : 0..6, retry : 0..6]
    /\ attempts \in [IDs -> States \cup {"NONE"}]
    /\ physical \in [IDs -> Kinds]
    /\ circuit \in [failures : 0..2, until : 0..6]
    /\ circuitExpected \in [failures : 0..2, until : 0..6]
    /\ recovery \in 0..2 /\ charged \subseteq IDs
    /\ leaseSafe \in BOOLEAN /\ recoverySafe \in BOOLEAN
    /\ scheduleSafe \in BOOLEAN /\ expirySafe \in BOOLEAN
    /\ last \in {"init", "claim", "retry-equal", "claim-equal", "expired",
                  "recovered-complete", "recovered-unsent", "finish-equal", "settled"}
LeaseSettlementFenced == leaseSafe
RecoveryCAS == recoverySafe
ScheduleRespected == scheduleSafe
ReconcileStrictExpiry == expirySafe
CircuitAccounting == circuit = circuitExpected
AttemptAccounting == job.count = Cardinality(charged)
AtomicJobAttempt == job.id = 0 \/ attempts[job.id] = IF job.status = "RETRY" THEN "ERROR" ELSE job.status
CompleteObserved == job.status = "COMPLETE" => physical[job.id] = "complete"

NoFinishAtEquality == last # "finish-equal"
NoClaimExpiryAtEquality == last # "claim-equal"
NoRecoveredCompletion == last # "recovered-complete"
NoRecoveredUnsent == last # "recovered-unsent"
NoRetryAtEquality == last # "retry-equal"
=============================================================================
