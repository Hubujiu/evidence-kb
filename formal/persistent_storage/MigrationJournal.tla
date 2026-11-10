-------------------------- MODULE MigrationJournal --------------------------
EXTENDS Naturals, TLC
CONSTANTS Mysql, RejectDirty, RejectHash, RejectDowngrade, MarkDirty,
          ClearAfterSteps, GrantAfterCommit, CloseAlways, InvalidateReleaseError, PreservePrimary, PublishIdentityAfterCommit
VARIABLES durable, working, effects, pendingEffects, pc, version, step,
          requested, digest, attempts, committed, closed, sessionSafe, granted, reason,
          badAdmission, badException, identity, published, failed, retryClean
Versions == 1..2
Empty == [present |-> FALSE, hash |-> "none", dirty |-> FALSE, step |-> 0]
Blank == [v \in Versions |-> Empty]
Zero == [v \in Versions |-> 0]
vars == <<durable, working, effects, pendingEffects, pc, version, step,
          requested, digest, attempts, committed, closed, sessionSafe, granted, reason,
          badAdmission, badException, identity, published, failed, retryClean>>
Init == /\ durable = Blank /\ working = Blank /\ effects = Zero /\ pendingEffects = Zero
        /\ pc = "initialize" /\ version = 1 /\ step = 1 /\ requested = 1 /\ digest = "A"
        /\ attempts = 0 /\ committed = FALSE /\ closed = TRUE /\ sessionSafe = TRUE /\ granted = FALSE
        /\ reason = "none" /\ badException = FALSE /\ badAdmission = FALSE /\ identity = "none" /\ published = "none"
        /\ failed = FALSE /\ retryClean = FALSE
\* Identity validation is in the migration transaction. A lost commit ACK can
\* leave durable identity but cannot publish an in-memory successful identity.
Initialize(expected, outcome) ==
    /\ pc = "initialize"
    /\ LET valid == expected = "none" \/ (identity = "dataset" /\ expected = "dataset")
           persisted == valid /\ outcome \in {"success", "lost"}
       IN /\ identity' = IF persisted THEN "dataset" ELSE identity
          /\ published' = IF valid /\ (outcome = "success" \/ ~PublishIdentityAfterCommit)
                          THEN "dataset" ELSE "none"
          /\ pc' = IF valid /\ outcome = "success" THEN "idle" ELSE "initialize-done"
    /\ UNCHANGED <<durable, working, effects, pendingEffects, version, step, requested,
        digest, attempts, committed, closed, sessionSafe, granted, reason, badAdmission, failed, retryClean, badException>>
RetryIdentity == /\ pc = "initialize-done" /\ published = "none" /\ identity = "dataset"
                /\ pc' = "initialize"
                /\ UNCHANGED <<durable, working, effects, pendingEffects, version, step, requested,
                    digest, attempts, committed, closed, sessionSafe, granted, reason, badAdmission,
                    identity, published, failed, retryClean, badException>>
Start(n, h, ownerOK) ==
    /\ pc \in {"idle", "done"} /\ attempts < 2 /\ published = "dataset"
    /\ LET downgrade == \E v \in Versions: durable[v].present /\ v > n
           rejected == ~ownerOK \/ (RejectDowngrade /\ downgrade)
       IN /\ pc' = IF rejected THEN "cleanup" ELSE "check"
          /\ reason' = IF ~ownerOK THEN "owner" ELSE IF rejected THEN "downgrade" ELSE "none"
          /\ badAdmission' = (badAdmission \/ (~rejected /\ downgrade))
    /\ working' = durable /\ pendingEffects' = effects /\ requested' = n /\ digest' = h
    /\ attempts' = attempts + 1 /\ version' = 1 /\ step' = 1
    /\ closed' = FALSE /\ sessionSafe' = FALSE /\ committed' = FALSE /\ granted' = FALSE
    /\ UNCHANGED <<durable, effects, identity, published, failed, retryClean, badException>>
Check ==
    /\ pc = "check"
    /\ IF version > requested THEN
           /\ pc' = "final-commit" /\ UNCHANGED <<reason, version, badAdmission, retryClean, badException>>
       ELSE LET row == working[version]
                wrongHash == row.present /\ row.hash # digest
                dirty == row.present /\ row.dirty
                rejected == (RejectHash /\ wrongHash) \/ (RejectDirty /\ dirty)
            IN /\ pc' = IF rejected THEN "cleanup" ELSE IF row.present THEN "check" ELSE "insert"
               /\ reason' = IF rejected THEN (IF wrongHash THEN "hash" ELSE "dirty") ELSE reason
               /\ version' = IF row.present /\ ~rejected THEN version + 1 ELSE version
               /\ badAdmission' = (badAdmission \/ (~rejected /\ (wrongHash \/ dirty)))
               /\ retryClean' = (retryClean \/ (failed /\ row.present /\ ~rejected /\ ~dirty /\ ~wrongHash))
    /\ UNCHANGED <<durable, working, effects, pendingEffects, step, requested, digest,
        attempts, committed, closed, sessionSafe, granted, identity, published, failed, badException>>
Insert == /\ pc = "insert"
          /\ working' = [working EXCEPT ![version] = [present |-> TRUE, hash |-> digest,
                                                       dirty |-> MarkDirty, step |-> 0]]
          /\ step' = 1 /\ pc' = IF Mysql THEN "dirty-commit" ELSE "statement"
          /\ UNCHANGED <<durable, effects, pendingEffects, version, requested, digest,
              attempts, committed, closed, sessionSafe, granted, reason, badAdmission, badException, identity, published, failed, retryClean>>
MysqlCommit == /\ Mysql /\ pc \in {"dirty-commit", "step-commit", "clean-commit"}
               /\ durable' = working /\ effects' = pendingEffects
               /\ pc' = CASE pc = "dirty-commit" -> "statement"
                             [] pc = "step-commit" -> IF step = 2 THEN "clean" ELSE "statement"
                             [] OTHER -> "check"
               /\ step' = IF pc = "step-commit" /\ step < 2 THEN step + 1 ELSE step
               /\ version' = IF pc = "clean-commit" THEN version + 1 ELSE version
               /\ UNCHANGED <<working, pendingEffects, requested, digest, attempts, committed, closed, sessionSafe,
                   granted, reason, badAdmission, badException, identity, published, failed, retryClean>>
\* MySQL DDL may be durable before step bookkeeping; SQLite/PG DDL remains
\* transactional. Engine atomicity/DDL semantics are assumptions, not proved here.
Statement == /\ pc = "statement"
             /\ pendingEffects' = [pendingEffects EXCEPT ![version] = step]
             /\ effects' = IF Mysql THEN [effects EXCEPT ![version] = step] ELSE effects
             /\ pc' = "step"
             /\ UNCHANGED <<durable, working, version, step, requested, digest, attempts, committed,
                 closed, sessionSafe, granted, reason, badAdmission, badException, identity, published, failed, retryClean>>
Step == /\ pc = "step"
        /\ working' = [working EXCEPT ![version].step = step]
        /\ pc' = IF Mysql THEN "step-commit" ELSE IF step = 2 THEN "clean" ELSE "statement"
        /\ step' = IF ~Mysql /\ step < 2 THEN step + 1 ELSE step
        /\ UNCHANGED <<durable, effects, pendingEffects, version, requested, digest, attempts,
            committed, closed, sessionSafe, granted, reason, badAdmission, badException, identity, published, failed, retryClean>>
Clean == /\ (pc = "clean" \/ (~ClearAfterSteps /\ pc = "statement"))
         /\ working' = [working EXCEPT ![version].dirty = FALSE]
         /\ pc' = IF Mysql THEN "clean-commit" ELSE "check"
         /\ version' = IF Mysql THEN version ELSE version + 1
         /\ UNCHANGED <<durable, effects, pendingEffects, step, requested, digest, attempts,
             committed, closed, sessionSafe, granted, reason, badAdmission, badException, identity, published, failed, retryClean>>
Commit(ack) == /\ pc = "final-commit"
               /\ durable' = working /\ effects' = pendingEffects /\ committed' = ack
               /\ pc' = "cleanup" /\ failed' = (failed \/ ~ack)
               /\ reason' = IF ack THEN reason ELSE "commit"
               /\ UNCHANGED <<working, pendingEffects, version, step, requested, digest, attempts,
                   closed, sessionSafe, granted, badAdmission, identity, published, retryClean, badException>>
Fail == /\ pc \in {"check", "insert", "dirty-commit", "statement", "step", "step-commit",
                         "clean", "clean-commit", "final-commit"}
        /\ working' = durable /\ pendingEffects' = effects /\ pc' = "cleanup" /\ failed' = TRUE
        /\ reason' = "failure"
        /\ UNCHANGED <<durable, effects, version, step, requested, digest, attempts, committed,
            closed, sessionSafe, granted, badAdmission, identity, published, retryClean, badException>>
Cleanup(releaseError) ==
    /\ pc = "cleanup"
    /\ closed' = (CloseAlways \/ ~releaseError \/ ~Mysql)
    /\ sessionSafe' = (~Mysql \/ ~releaseError \/ InvalidateReleaseError)
    /\ badException' = (badException \/ (Mysql /\ releaseError /\ reason # "none" /\ ~PreservePrimary))
    /\ pc' = IF committed /\ ~(Mysql /\ releaseError) THEN "grant" ELSE "done"
    /\ UNCHANGED <<durable, working, effects, pendingEffects, version, step, requested, digest,
        attempts, committed, granted, reason, badAdmission, identity, published, failed, retryClean>>
Grant(ok) == /\ (pc = "grant" \/ (~GrantAfterCommit /\ pc = "insert"))
             /\ granted' = ok /\ pc' = "done" /\ failed' = (failed \/ ~ok)
             /\ UNCHANGED <<durable, working, effects, pendingEffects, version, step, requested,
                 digest, attempts, committed, closed, sessionSafe, reason, badAdmission, identity, published, retryClean, badException>>
Next == (\E e \in {"none", "dataset", "other"}, o \in {"success", "lost", "failed"}: Initialize(e, o))
        \/ RetryIdentity \/ (\E n \in Versions, h \in {"A", "B"}, ownerOK \in BOOLEAN: Start(n, h, ownerOK))
        \/ Check \/ Insert \/ MysqlCommit \/ Statement \/ Step \/ Clean
        \/ (\E ack \in BOOLEAN: Commit(ack)) \/ Fail
        \/ (\E err \in BOOLEAN: Cleanup(err)) \/ (\E ok \in BOOLEAN: Grant(ok))
Spec == Init /\ [][Next]_vars
Row == [present: BOOLEAN, hash: {"none", "A", "B"}, dirty: BOOLEAN, step: 0..2]
TypeOK == /\ durable \in [Versions -> Row] /\ working \in [Versions -> Row]
          /\ effects \in [Versions -> 0..2] /\ pendingEffects \in [Versions -> 0..2]
          /\ version \in 1..3 /\ step \in 1..2 /\ attempts \in 0..2
          /\ identity \in {"none", "dataset"} /\ published \in {"none", "dataset"}
          /\ requested \in Versions /\ digest \in {"A", "B"}
          /\ pc \in {"initialize", "initialize-done", "idle", "check", "insert", "dirty-commit",
                        "statement", "step", "step-commit", "clean", "clean-commit", "final-commit",
                        "cleanup", "grant", "done"}
          /\ reason \in {"none", "owner", "downgrade", "hash", "dirty", "commit", "failure"}
          /\ {committed, closed, sessionSafe, granted, badAdmission, badException, failed, retryClean} \subseteq BOOLEAN
IdentityPublication == published # "none" => identity = published
RefuseIncompatibleJournal == ~badAdmission
CleanMeansComplete == \A v \in Versions: durable[v].present /\ ~durable[v].dirty =>
                      durable[v].step = 2 /\ effects[v] = 2
PartialEffectHasDirtyJournal == \A v \in Versions: effects[v] > 0 => durable[v].present
                               /\ (effects[v] > durable[v].step => durable[v].dirty)
RuntimeAfterDurability == granted => committed /\ closed /\
                         (\A v \in 1..requested: durable[v].present /\ ~durable[v].dirty)
ConnectionAlwaysClosed == pc \in {"done", "grant"} => closed
ReleasedSessionNotReused == pc \in {"done", "grant"} => sessionSafe
PrimaryErrorPreserved == ~badException
NoUpgradeWitness == ~(granted /\ requested = 2)
NoDirtyRefusalWitness == reason # "dirty"
NoDurableRetryWitness == ~retryClean
NoLostIdentityWitness == ~(identity = "dataset" /\ published = "none")
=============================================================================
