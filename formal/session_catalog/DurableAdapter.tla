---- MODULE DurableAdapter ----
EXTENDS Naturals, TLC
CONSTANTS CheckNamespace, CloseAfterAck, PreserveCommitted, ReplayWithoutEffects, BlockUnknown
VARIABLE s
Init == \E kind \in BOOLEAN :
    s = [kind |-> kind, phase |-> "new", status |-> "absent", result |-> "none", turn |-> 0,
         user |-> FALSE, tool |-> FALSE, pending |-> FALSE, effects |-> 0,
         candidate |-> FALSE, final |-> FALSE, committedSuccess |-> FALSE,
         checked |-> FALSE, cancelled |-> FALSE, closed |-> FALSE, public |-> "none",
         persistenceError |-> FALSE, replayed |-> FALSE, mode |-> "fresh",
         cached |-> "none", replayEffects |-> 0, attemptedNew |-> FALSE, unsafeEffect |-> FALSE]
Begin == /\ s.phase = "new" /\ (s.kind \/ ~CheckNamespace)
         /\ s' = [s EXCEPT !.phase = "starting", !.status = "running"]
RejectNamespace == /\ s.phase = "new" /\ ~s.kind /\ CheckNamespace
                   /\ s' = [s EXCEPT !.phase = "rejected"]
RecordUser == /\ s.phase = "starting"
              /\ s' = [s EXCEPT !.phase = "running", !.user = TRUE]
LoseUserAck == /\ s.phase = "starting"
               /\ s' = [s EXCEPT !.phase = "cleanup", !.user = TRUE, !.persistenceError = TRUE]
StartFails == /\ s.phase = "starting"
              /\ s' = [s EXCEPT !.phase = "cleanup", !.persistenceError = TRUE]
RecordTool == /\ s.phase = "running" /\ ~s.tool
              /\ s' = [s EXCEPT !.phase = "tool", !.tool = TRUE, !.pending = TRUE]
LoseToolAck == /\ s.phase = "tool"
               /\ s' = [s EXCEPT !.phase = "cleanup", !.persistenceError = TRUE]
ExecuteTool == /\ s.phase = "tool"
               /\ s' = [s EXCEPT !.phase = "tool-result", !.effects = @ + 1]
RecordToolResult == /\ s.phase = "tool-result"
                    /\ s' = [s EXCEPT !.phase = "running", !.pending = FALSE]
UnknownToolResult == /\ s.phase = "tool-result"
                     /\ s' = [s EXCEPT !.phase = "cleanup"]
StageFinal == /\ s.phase = "running"
              /\ s' = [s EXCEPT !.phase = "finish-ready", !.candidate = TRUE]
Cancel == /\ s.phase \in {"finish-ready", "finish-checked"} /\ ~s.cancelled
          /\ s' = [s EXCEPT !.cancelled = TRUE]
CheckFinish == /\ s.phase = "finish-ready" /\ ~s.cancelled
               /\ s' = [s EXCEPT !.phase = "finish-checked", !.checked = TRUE,
                         !.closed = IF CloseAfterAck THEN FALSE ELSE TRUE]
RejectCancelled == /\ s.phase = "finish-ready" /\ s.cancelled
                   /\ s' = [s EXCEPT !.phase = "cleanup"]
CommitFinish == /\ s.phase = "finish-checked"
                /\ s' = [s EXCEPT !.phase = "finish-committed", !.status = "complete", !.result = "success",
                          !.turn = 1, !.final = s.candidate, !.committedSuccess = TRUE]
FailBeforeCommit == /\ s.phase = "finish-checked"
                    /\ s' = [s EXCEPT !.phase = "cleanup", !.persistenceError = TRUE]
AcknowledgeFinish == /\ s.phase = "finish-committed"
                     /\ s' = [s EXCEPT !.phase = "closed", !.closed = TRUE, !.candidate = FALSE, !.public = "success"]
LoseFinishAck == /\ s.phase = "finish-committed"
                 /\ s' = [s EXCEPT !.phase = "cleanup", !.public = "unknown", !.persistenceError = TRUE]
FailRunning == /\ s.phase = "cleanup" /\ s.status = "running"
               /\ s' = [s EXCEPT !.phase = "closed", !.closed = TRUE, !.candidate = FALSE,
                         !.status = "complete", !.result = "error",
                         !.public = IF s.persistenceError THEN "unknown" ELSE "error"]
RejectTerminalCleanup == /\ s.phase = "cleanup" /\ s.status = "complete"
                         /\ s' = [s EXCEPT !.phase = "uncertain", !.public = "unknown",
                                   !.result = IF PreserveCommitted THEN s.result ELSE "error",
                                   !.final = IF PreserveCommitted THEN s.final ELSE FALSE]
OpenReplay == /\ s.phase \in {"closed", "uncertain", "blocked"} /\ s.status = "complete" /\ ~s.replayed
              /\ s' = [s EXCEPT !.phase = "replay", !.closed = FALSE, !.mode = "replay",
                        !.replayed = TRUE, !.cached = s.result, !.replayEffects = s.effects, !.public = "none"]
FinishReplay == /\ s.phase = "replay"
                /\ s' = [s EXCEPT !.phase = "closed", !.closed = TRUE, !.public = s.cached,
                          !.effects = IF ReplayWithoutEffects THEN s.effects ELSE s.effects + 1]
AttemptNew == /\ s.phase \in {"closed", "uncertain"} /\ s.pending /\ ~s.attemptedNew
              /\ s' = [s EXCEPT !.phase = IF BlockUnknown THEN "blocked" ELSE "unsafe-new",
                        !.attemptedNew = TRUE, !.unsafeEffect = ~BlockUnknown,
                        !.effects = IF BlockUnknown THEN s.effects ELSE s.effects + 1]
Next == Begin \/ RejectNamespace \/ RecordUser \/ LoseUserAck \/ StartFails
        \/ RecordTool \/ LoseToolAck \/ ExecuteTool \/ RecordToolResult \/ UnknownToolResult
        \/ StageFinal \/ Cancel \/ CheckFinish \/ RejectCancelled \/ CommitFinish \/ FailBeforeCommit
        \/ AcknowledgeFinish \/ LoseFinishAck \/ FailRunning \/ RejectTerminalCleanup
        \/ OpenReplay \/ FinishReplay \/ AttemptNew
Spec == Init /\ [][Next]_s
TypeOK == /\ s.kind \in BOOLEAN /\ s.status \in {"absent", "running", "complete"}
          /\ s.phase \in {"new", "starting", "rejected", "running", "tool", "tool-result", "finish-ready",
                         "finish-checked", "finish-committed", "cleanup", "closed", "uncertain", "replay", "blocked", "unsafe-new"}
          /\ s.result \in {"none", "success", "error"} /\ s.public \in {"none", "unknown", "success", "error"}
          /\ s.turn \in 0..1 /\ s.effects \in 0..3 /\ s.replayEffects \in 0..2
          /\ s.cached \in {"none", "success", "error"} /\ s.mode \in {"fresh", "replay"}
          /\ \A field \in {"user", "tool", "pending", "candidate", "final", "committedSuccess", "checked", "cancelled",
                           "closed", "persistenceError", "replayed", "attemptedNew", "unsafeEffect"} : s[field] \in BOOLEAN
NamespaceAdmission == s.status # "absent" => s.kind
ClosedRequiresDurable == s.closed => s.status = "complete"
FinalSharesCommit == s.final => s.status = "complete" /\ s.result = "success" /\ s.turn = 1
SuccessRequiresCommit == s.public = "success" => s.status = "complete" /\ s.result = "success" /\ s.final /\ s.turn = 1
CommittedSuccessStable == s.committedSuccess => s.result = "success" /\ s.final /\ s.turn = 1
FreshFinishChecked == s.committedSuccess => s.checked
ReplayHasNoEffects == (s.mode = "replay" /\ s.phase = "closed") => s.effects = s.replayEffects
UnknownBlocksNew == ~s.unsafeEffect
NoSuccessWitness == ~(s.phase = "closed" /\ s.mode = "fresh" /\ s.public = "success" /\ s.final /\ s.turn = 1)
NoCommitUnknownWitness == ~(s.phase = "uncertain" /\ s.status = "complete" /\ s.result = "success" /\ ~s.closed /\ s.public = "unknown")
NoReplayWitness == ~(s.phase = "closed" /\ s.mode = "replay" /\ s.public = "success" /\ s.effects = s.replayEffects)
NoUnknownToolBlockedWitness == ~(s.phase = "blocked" /\ s.pending /\ s.effects = 1 /\ s.result = "error")
NoToolRecordUnknownWitness == ~(s.phase = "closed" /\ s.pending /\ s.effects = 0 /\ s.public = "unknown" /\ s.status = "complete")
NoCancellationRejectedWitness == ~(s.phase = "closed" /\ s.cancelled /\ s.result = "error" /\ ~s.final)
NoRecoveredCommitWitness == ~(s.phase = "closed" /\ s.mode = "replay" /\ s.public = "success" /\ s.persistenceError /\ s.effects = s.replayEffects)
NoUnknownToolReplayWitness == ~(s.phase = "closed" /\ s.mode = "replay" /\ s.pending /\ s.effects = 1 /\ s.public = "error" /\ s.effects = s.replayEffects)
====
