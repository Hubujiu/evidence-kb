--------------------------- MODULE KernelExecution ---------------------------
EXTENDS Naturals, TLC

CONSTANTS CheckCorrelation, CheckTerminal, CheckDeadline
MaxOperations == 2
Deadline == 2
Times == 0..3
Kinds == {"model", "tool", "host"}
Terminal == {"completed", "cancelled", "failed", "expired", "limit"}
Events == [kind : {"dispatch", "settle", "finish", "cancel", "fail", "invalid"},
           operation : Kinds \cup {"none", "invalid"}, id : 0..3, now : Times]
VARIABLES state, terminalStable, matchedSettlement, deadlineEnforced,
          rejectedStable, countMonotone, timeMonotone
vars == <<state, terminalStable, matchedSettlement, deadlineEnforced,
          rejectedStable, countMonotone, timeMonotone>>

Stop(s, p, t) == [s EXCEPT !.phase = p, !.pendingKind = "none",
                           !.pendingId = 0, !.now = t]
Result(s, code) == [state |-> s, status |-> code]
ShapeOK(e) ==
    IF e.kind \in {"dispatch", "settle"}
    THEN e.operation \in Kinds /\ e.id # 0
    ELSE e.kind \in {"finish", "cancel", "fail"}
         /\ e.operation = "none" /\ e.id = 0

(* The priority of these guards mirrors transition(), including the fact that
   invalid/corrupt/stale events do not change a state even past its deadline. *)
Apply(s, e) ==
    IF ~ShapeOK(e) THEN Result(s, "reject")
    ELSE IF CheckTerminal /\ s.phase \in Terminal THEN Result(s, "reject")
    ELSE IF e.now < s.now THEN Result(s, "reject")
    ELSE IF e.kind = "dispatch" /\ s.phase = "waiting" THEN Result(s, "reject")
    ELSE IF e.kind = "dispatch" /\ s.count < MaxOperations /\ e.id # s.count + 1
         THEN Result(s, "reject")
    ELSE IF e.kind = "settle" /\ s.phase # "waiting" THEN Result(s, "reject")
    ELSE IF e.kind = "settle" /\ CheckCorrelation
            /\ (e.id # s.pendingId \/ e.operation # s.pendingKind)
         THEN Result(s, "reject")
    ELSE IF e.kind = "finish" /\ s.phase = "waiting" THEN Result(s, "reject")
    ELSE IF CheckDeadline /\ e.now >= Deadline
         THEN Result(Stop(s, "expired", e.now), "deadline")
    ELSE IF e.kind = "dispatch" /\ s.count = MaxOperations
         THEN Result(Stop(s, "limit", e.now), "limit")
    ELSE IF e.kind = "dispatch"
         THEN Result([s EXCEPT !.phase = "waiting", !.count = @ + 1,
                              !.pendingId = e.id, !.pendingKind = e.operation,
                              !.now = e.now], "ok")
    ELSE IF e.kind = "settle" THEN Result(Stop(s, "ready", e.now), "ok")
    ELSE Result(Stop(s, CASE e.kind = "finish" -> "completed"
                         [] e.kind = "cancel" -> "cancelled"
                         [] OTHER -> "failed", e.now), "ok")

Init ==
    /\ state = [phase |-> "ready", count |-> 0, pendingId |-> 0,
                 pendingKind |-> "none", now |-> 0]
    /\ terminalStable = TRUE /\ matchedSettlement = TRUE
    /\ deadlineEnforced = TRUE /\ rejectedStable = TRUE
    /\ countMonotone = TRUE /\ timeMonotone = TRUE

Step(e) ==
    LET result == Apply(state, e)
        next == result.state
    IN /\ state' = next
       /\ terminalStable' = (state.phase \in Terminal => next = state)
       /\ matchedSettlement' =
            (e.kind = "settle" /\ result.status = "ok" =>
               state.phase = "waiting" /\ e.id = state.pendingId
               /\ e.operation = state.pendingKind)
       /\ deadlineEnforced' = (result.status = "ok" => e.now < Deadline)
       /\ rejectedStable' = (result.status = "reject" => next = state)
       /\ countMonotone' = (next.count >= state.count)
       /\ timeMonotone' = (next.now >= state.now)

Next == \E e \in Events : Step(e)
Spec == Init /\ [][Next]_vars
TypeOK == state \in [phase : {"ready", "waiting"} \cup Terminal,
                    count : 0..MaxOperations, pendingId : 0..MaxOperations,
                    pendingKind : Kinds \cup {"none"}, now : Times]
PendingMatches ==
    IF state.phase = "waiting"
    THEN state.pendingId = state.count /\ state.pendingId # 0
         /\ state.pendingKind \in Kinds
    ELSE state.pendingId = 0 /\ state.pendingKind = "none"
ExpiredClock == IF state.phase = "expired" THEN state.now >= Deadline
                ELSE state.now < Deadline
LimitReached == state.phase = "limit" => state.count = MaxOperations
TerminalAbsorbing == terminalStable
SettlementCorrelated == matchedSettlement
DeadlineEnforced == deadlineEnforced
RejectsPreserveState == rejectedStable
CountersMonotone == countMonotone /\ timeMonotone
=============================================================================
