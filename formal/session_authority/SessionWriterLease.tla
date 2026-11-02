-------------------------- MODULE SessionWriterLease --------------------------
EXTENDS Naturals, FiniteSets, Sequences, TLC

CONSTANTS Requests, Fingerprints, Nil, FenceWriters, AtomicCommit
ASSUME /\ Requests # {} /\ Fingerprints # {}
       /\ Nil \notin Requests /\ Nil \notin Fingerprints
       /\ FenceWriters \in BOOLEAN /\ AtomicCommit \in BOOLEAN

VARIABLES status, fingerprint, live, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, observation,
          acquiredRevision, lockOwner, finishSuccess, finishHasAssistant, admissionLive,
          admissionStatus, pendingReply, replays, appendWitnesses

vars == <<status, fingerprint, live, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, observation,
          acquiredRevision, lockOwner, finishSuccess, finishHasAssistant, admissionLive,
          admissionStatus, pendingReply, replays, appendWitnesses>>

Statuses == {"absent", "running", "interrupted", "complete"}
Observations == {"none", "pending", "unknown", "known"}
Running == {r \in Requests : status[r] = "running"}
Complete == {r \in Requests : status[r] = "complete"}
LiveLease(r) == status[r] = "running" /\ live[r]
CompletedSet == {completed[i] : i \in 1..Len(completed)}

Init ==
    /\ status = [r \in Requests |-> "absent"]
    /\ fingerprint = [r \in Requests |-> Nil]
    /\ live = [r \in Requests |-> FALSE]
    /\ revision = 0 /\ completed = <<>> /\ lastState = Nil
    /\ responseRevision = [r \in Requests |-> 0]
    /\ assistants = {} /\ expectedAssistants = {} /\ successful = {} /\ superseded = {}
    /\ observation = [r \in Requests |-> "none"]
    /\ acquiredRevision = [r \in Requests |-> 0]
    /\ lockOwner = Nil /\ finishSuccess = FALSE /\ finishHasAssistant = FALSE
    /\ admissionLive = [r \in Requests |-> FALSE]
    /\ admissionStatus = [r \in Requests |-> "absent"]
    /\ pendingReply = {} /\ replays = {} /\ appendWitnesses = {}

\* Each request denotes a unique token, scoped to one immutable session owner.
\* begin owns the session row lock for its whole transaction. A rejected call
\* is a stuttering step: it does not recreate an existing request or run work.
Begin(r, fp, expected) ==
    /\ lockOwner = Nil /\ status[r] = "absent" /\ expected = revision
    /\ \A q \in Running : ~live[q]
    /\ status' = [q \in Requests |-> IF q = r THEN "running"
                     ELSE IF q \in Running THEN "interrupted" ELSE status[q]]
    /\ superseded' = superseded \cup Running
    /\ fingerprint' = [fingerprint EXCEPT ![r] = fp]
    /\ live' = [live EXCEPT ![r] = TRUE]
    /\ acquiredRevision' = [acquiredRevision EXCEPT ![r] = revision]
    /\ UNCHANGED <<revision, completed, responseRevision, lastState, assistants,
          expectedAssistants, successful, observation, lockOwner, finishSuccess, finishHasAssistant, admissionLive,
          admissionStatus, pendingReply, replays, appendWitnesses>>

Expire(r) ==
    /\ status[r] = "running" /\ live[r]
    /\ live' = [live EXCEPT ![r] = FALSE]
    /\ UNCHANGED <<status, fingerprint, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, observation,
          acquiredRevision, lockOwner, finishSuccess, finishHasAssistant, admissionLive,
          admissionStatus, pendingReply, replays, appendWitnesses>>

AppendEvent(r) ==
    /\ lockOwner = Nil /\ LiveLease(r)
    /\ appendWitnesses' = appendWitnesses \cup {<<r, status[r], live[r]>>}
    /\ UNCHANGED <<status, fingerprint, live, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, observation,
          acquiredRevision, lockOwner, finishSuccess, finishHasAssistant, admissionLive,
          admissionStatus, pendingReply, replays>>

\* _finish first locks the session and validates the token. Failure may close
\* an expired running request, but not one superseded by a newer writer.
\* The mutation switch is a negative control, never a production option.
PrepareFinish(r, success, includeAssistant) ==
    /\ lockOwner = Nil
    /\ status[r] = "running" \/ (~FenceWriters /\ status[r] = "interrupted")
    /\ ~success \/ live[r]
    /\ ~includeAssistant \/ success
    /\ lockOwner' = r /\ finishSuccess' = success
    /\ finishHasAssistant' = includeAssistant
    /\ admissionLive' = [admissionLive EXCEPT ![r] = live[r]]
    /\ admissionStatus' = [admissionStatus EXCEPT ![r] = status[r]]
    /\ observation' = [observation EXCEPT ![r] = "pending"]
    /\ UNCHANGED <<status, fingerprint, live, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, acquiredRevision,
          pendingReply, replays, appendWitnesses>>

\* The adapter supplies atomic commit/rollback and row-lock exclusion. Clock
\* expiry can interleave here, but a successor begin cannot acquire the lock.
Commit ==
    /\ lockOwner # Nil
    /\ LET r == lockOwner IN
       /\ status' = [status EXCEPT ![r] = "complete"]
       /\ responseRevision' = [responseRevision EXCEPT ![r] = revision + 1]
       /\ completed' = Append(completed, r)
       /\ lastState' = r
       /\ assistants' = IF finishHasAssistant THEN assistants \cup {r} ELSE assistants
       /\ expectedAssistants' = IF finishHasAssistant THEN expectedAssistants \cup {r}
                                  ELSE expectedAssistants
       /\ successful' = IF finishSuccess THEN successful \cup {r} ELSE successful
       /\ pendingReply' = pendingReply \cup {r}
    /\ revision' = revision + 1 /\ lockOwner' = Nil
    /\ UNCHANGED <<fingerprint, live, superseded, observation, acquiredRevision,
          finishSuccess, finishHasAssistant, admissionLive, admissionStatus, replays, appendWitnesses>>

RollbackBeforeCommit ==
    /\ lockOwner # Nil
    /\ observation' = [observation EXCEPT ![lockOwner] = "unknown"]
    /\ lockOwner' = Nil
    /\ UNCHANGED <<status, fingerprint, live, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, acquiredRevision,
          finishSuccess, finishHasAssistant, admissionLive, admissionStatus, pendingReply,
          replays, appendWitnesses>>

ObserveReply(r, received) ==
    /\ r \in pendingReply
    /\ observation' = [observation EXCEPT ![r] = IF received THEN "known" ELSE "unknown"]
    /\ pendingReply' = pendingReply \ {r}
    /\ UNCHANGED <<status, fingerprint, live, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, acquiredRevision,
          lockOwner, finishSuccess, finishHasAssistant, admissionLive, admissionStatus,
          replays, appendWitnesses>>

\* Completed replay intentionally ignores expected_revision, but never the
\* original fingerprint. Unknown post-commit outcomes can thus be reconciled.
Replay(r, fp) ==
    /\ status[r] = "complete" /\ fp = fingerprint[r]
    /\ observation' = [observation EXCEPT ![r] = "known"]
    /\ pendingReply' = pendingReply \ {r}
    /\ replays' = replays \cup {<<r, fp>>}
    /\ UNCHANGED <<status, fingerprint, live, revision, completed, responseRevision,
          lastState, assistants, expectedAssistants, successful, superseded, acquiredRevision,
          lockOwner, finishSuccess, finishHasAssistant, admissionLive, admissionStatus, appendWitnesses>>

ExposePartialAssistant ==
    /\ ~AtomicCommit /\ lockOwner # Nil /\ finishHasAssistant
    /\ assistants' = assistants \cup {lockOwner}
    /\ UNCHANGED <<status, fingerprint, live, revision, completed, responseRevision,
          lastState, expectedAssistants, successful, superseded, observation, acquiredRevision,
          lockOwner, finishSuccess, finishHasAssistant, admissionLive, admissionStatus,
          pendingReply, replays, appendWitnesses>>

Next ==
    \/ \E r \in Requests, fp \in Fingerprints, expected \in 0..Cardinality(Requests) :
           Begin(r, fp, expected)
    \/ \E r \in Requests : Expire(r) \/ AppendEvent(r)
    \/ \E r \in Requests, success, includeAssistant \in BOOLEAN :
           PrepareFinish(r, success, includeAssistant)
    \/ Commit \/ RollbackBeforeCommit \/ ExposePartialAssistant
    \/ \E r \in Requests, received \in BOOLEAN : ObserveReply(r, received)
    \/ \E r \in Requests, fp \in Fingerprints : Replay(r, fp)

TypeOK ==
    /\ status \in [Requests -> Statuses]
    /\ fingerprint \in [Requests -> (Fingerprints \cup {Nil})]
    /\ live \in [Requests -> BOOLEAN]
    /\ revision \in 0..Cardinality(Requests)
    /\ completed \in Seq(Requests) /\ Len(completed) <= Cardinality(Requests)
    /\ responseRevision \in [Requests -> 0..Cardinality(Requests)]
    /\ lastState \in Requests \cup {Nil}
    /\ assistants \subseteq Requests /\ expectedAssistants \subseteq Requests
    /\ successful \subseteq Requests
    /\ superseded \subseteq Requests /\ pendingReply \subseteq Requests
    /\ observation \in [Requests -> Observations]
    /\ acquiredRevision \in [Requests -> 0..Cardinality(Requests)]
    /\ lockOwner \in Requests \cup {Nil} /\ finishSuccess \in BOOLEAN
    /\ finishHasAssistant \in BOOLEAN
    /\ admissionLive \in [Requests -> BOOLEAN]
    /\ admissionStatus \in [Requests -> Statuses]
    /\ replays \subseteq Requests \X Fingerprints
    /\ appendWitnesses \subseteq Requests \X Statuses \X BOOLEAN

SingleWriter == Cardinality(Running) <= 1
NoSupersededCompletion == Complete \intersect superseded = {}
AtomicCompletion ==
    /\ revision = Len(completed)
    /\ Complete = CompletedSet
    /\ assistants = expectedAssistants /\ assistants \subseteq successful
    /\ successful \subseteq Complete
    /\ lastState = IF revision = 0 THEN Nil ELSE completed[revision]
    /\ \A r \in Requests : (responseRevision[r] > 0) <=> (r \in Complete)
RevisionOrder ==
    /\ Cardinality(Complete) = Len(completed)
    /\ \A i \in 1..Len(completed) : responseRevision[completed[i]] = i
    /\ \A r \in Complete : responseRevision[r] = acquiredRevision[r] + 1
SuccessfulAdmission ==
    \A r \in successful : admissionLive[r] /\ admissionStatus[r] = "running"
FailureAdmission == \A r \in Complete : admissionStatus[r] = "running"
KnownMeansComplete == \A r \in Requests : observation[r] = "known" => r \in Complete
ReplayIdentity == \A pair \in replays : pair[2] = fingerprint[pair[1]]
AppendAdmission == appendWitnesses \subseteq Requests \X {"running"} \X {TRUE}

Spec == Init /\ [][Next]_vars
=============================================================================
