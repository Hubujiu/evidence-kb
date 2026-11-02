---------------------------- MODULE LoopSequencing ---------------------------
EXTENDS Naturals, TLC

CONSTANTS CheckCorrelation, RecordCall, RecordResult
MaxTools == 2
MaxEffects == 14
Phases == {"start", "context", "model", "append-call", "tool",
           "append-result", "append-answer", "complete", "closed"}
VARIABLES s, correlated, toolHadRecord, noReplay, terminalStable
vars == <<s, correlated, toolHadRecord, noReplay, terminalStable>>

Init ==
    /\ s \in {[phase |-> "start", pending |-> 0, sequence |-> 0,
                mode |-> mode, direct |-> direct, cached |-> FALSE,
                callRecorded |-> FALSE, resultRecorded |-> FALSE,
                answerRecorded |-> FALSE, tools |-> 0] :
                mode \in {"model-tools", "single-tool"}, direct \in BOOLEAN}
    /\ correlated = TRUE /\ toolHadRecord = TRUE
    /\ noReplay = TRUE /\ terminalStable = TRUE

Close(a) == [a EXCEPT !.phase = "closed", !.pending = 0]
AfterCall(a) == [a EXCEPT !.phase = IF RecordCall THEN "append-call" ELSE "tool",
                         !.callRecorded = FALSE, !.resultRecorded = FALSE]
AfterResult(a) ==
    [a EXCEPT !.tools = @ + 1,
       !.phase = IF RecordResult THEN "append-result"
                 ELSE IF a.direct \/ a.mode = "single-tool" THEN "complete"
                 ELSE "context"]
ValidResult(a, r) ==
    CASE a.phase = "start" -> r \in {"fresh", "cached"}
      [] a.phase = "context" -> r = "context"
      [] a.phase = "model" -> r \in {"answer", "tool"}
      [] a.phase \in {"append-call", "append-result", "append-answer", "tool"}
           -> r = "ack"
      [] OTHER -> FALSE

Advance(a, r) ==
    LET b == [a EXCEPT !.pending = 0]
    IN CASE a.phase = "start" ->
             IF r = "cached" THEN [b EXCEPT !.phase = "complete", !.cached = TRUE]
             ELSE IF a.mode = "single-tool" THEN AfterCall(b)
             ELSE [b EXCEPT !.phase = "context"]
         [] a.phase = "context" -> [b EXCEPT !.phase = "model"]
         [] a.phase = "model" ->
             IF r = "answer" THEN [b EXCEPT !.phase = "append-answer"]
             ELSE AfterCall(b)
         [] a.phase = "append-call" ->
             [b EXCEPT !.phase = "tool", !.callRecorded = TRUE]
         [] a.phase = "tool" -> AfterResult(b)
         [] a.phase = "append-result" ->
             [b EXCEPT !.resultRecorded = TRUE,
                !.phase = IF a.direct \/ a.mode = "single-tool" THEN "complete" ELSE "context"]
         [] OTHER -> [b EXCEPT !.phase = "complete", !.answerRecorded = TRUE]

Issue ==
    /\ s.phase # "closed"
    /\ LET next ==
          IF s.pending # 0 THEN Close(s)
          ELSE IF s.phase = "complete" THEN s
          ELSE [s EXCEPT !.sequence = @ + 1, !.pending = s.sequence + 1]
       IN /\ s' = next
          /\ toolHadRecord' = (next.pending # 0 /\ next.phase = "tool" => s.callRecorded)
          /\ noReplay' = (s.pending # 0 => next.phase = "closed" /\ next.sequence = s.sequence)
          /\ terminalStable' = (s.phase = "complete" => next = s)
    /\ UNCHANGED correlated

Accept(id, r) ==
    /\ s.phase # "closed"
    /\ ~(s.phase = "model" /\ r = "tool" /\ s.tools = MaxTools)
    /\ LET match == s.pending # 0 /\ s.pending = id
           valid == ValidResult(s, r)
           advance == s.pending # 0 /\ valid /\ (match \/ ~CheckCorrelation)
           next == IF advance THEN Advance(s, r) ELSE Close(s)
       IN /\ s' = next
          /\ correlated' = (advance => match)
    /\ UNCHANGED <<toolHadRecord, noReplay, terminalStable>>

CloseHandle ==
    /\ s.phase # "closed"
    /\ s' = Close(s)
    /\ UNCHANGED <<correlated, toolHadRecord, noReplay, terminalStable>>
Next == Issue \/ CloseHandle \/
    (\E id \in 0..MaxEffects, r \in {"fresh", "cached", "context", "answer", "tool", "ack", "invalid"} : Accept(id, r))
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ s.phase \in Phases /\ s.pending \in 0..MaxEffects /\ s.sequence \in 0..MaxEffects
    /\ s.tools \in 0..MaxTools
    /\ s.mode \in {"model-tools", "single-tool"}
    /\ s.direct \in BOOLEAN /\ s.cached \in BOOLEAN
    /\ s.callRecorded \in BOOLEAN /\ s.resultRecorded \in BOOLEAN /\ s.answerRecorded \in BOOLEAN
PendingIdentity == s.pending # 0 => s.pending = s.sequence /\ s.phase \notin {"closed", "complete"}
SettlementCorrelated == correlated
ToolRequiresCallRecord == toolHadRecord
ResultBeforeContinuation == s.phase \in {"context", "model"} /\ s.tools > 0 => s.resultRecorded
CompleteRequiresRecord == s.phase = "complete" =>
    s.cached \/ s.answerRecorded \/ ((s.direct \/ s.mode = "single-tool") /\ s.resultRecorded)
CachedHasNoNewWork == s.cached => s.tools = 0 /\ s.sequence = 1 /\ s.phase \in {"complete", "closed"}
SingleToolNeverModels == s.mode = "single-tool" => s.phase \notin {"context", "model"}
IssuedEffectNotReplayed == noReplay
CompleteNextStable == terminalStable
=============================================================================
