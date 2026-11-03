--------------------------- MODULE MemorySession ---------------------------
EXTENDS Naturals
CONSTANTS AllowClosedAppend, SkipClear
VARIABLE s
vars == <<s>>
Init == s = [closed |-> FALSE, records |-> 1, snapshot |-> 0, snapTaken |-> FALSE,
             appends |-> 0, badAppend |-> FALSE, finished |-> FALSE, failed |-> FALSE]
Append == /\ s.appends < 2 /\ (~s.closed \/ AllowClosedAppend)
          /\ s' = [s EXCEPT !.records = @ + 1, !.appends = @ + 1,
                     !.badAppend = (s.badAppend \/ s.closed)]
Snapshot == /\ ~s.snapTaken
            /\ s' = [s EXCEPT !.snapshot = s.records, !.snapTaken = TRUE]
Finish == /\ ~s.finished
          /\ s' = [s EXCEPT !.closed = TRUE, !.finished = TRUE,
                     !.records = IF SkipClear THEN s.records ELSE 0]
Fail == /\ ~s.failed
        /\ s' = [s EXCEPT !.closed = TRUE, !.failed = TRUE,
                   !.records = IF SkipClear THEN s.records ELSE 0]
Next == Append \/ Snapshot \/ Finish \/ Fail
Spec == Init /\ [][Next]_vars
TypeOK == /\ s.closed \in BOOLEAN /\ s.records \in 0..3 /\ s.snapshot \in 0..3
          /\ s.appends \in 0..2 /\ s.badAppend \in BOOLEAN
NoClosedAppend == ~s.badAppend
ClosedClearsRecords == s.closed => s.records = 0
WitnessFinishedSnapshot == ~(s.finished /\ s.snapTaken /\ s.snapshot > 0 /\ s.records = 0)
WitnessFailure == ~(s.failed /\ s.closed /\ s.records = 0)
=============================================================================
