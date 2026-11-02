------------------------- MODULE LlmStreamKnowledge -------------------------
EXTENDS Naturals, Sequences
CONSTANTS PreserveTerminalUsage, ReleaseOnTerminal
VARIABLES queue, produced, block, room, observed, assembled, knownAtTerminal,
          published, terminal, providerFinished, dispatched, taskDone, lease,
          cancelled, closed, terminalCount
vars == <<queue, produced, block, room, observed, assembled, knownAtTerminal,
          published, terminal, providerFinished, dispatched, taskDone, lease,
          cancelled, closed, terminalCount>>
Events == {"start", "tool", "delta", "validTool", "usageHigh", "usageLow", "finish"}
Usage(e) == IF e = "usageHigh" THEN 1 ELSE IF e = "usageLow" THEN 2 ELSE 0
Init == /\ queue = <<>> /\ produced = 0 /\ block = "none" /\ room = 2
        /\ observed = 0 /\ assembled = 0 /\ knownAtTerminal = 0
        /\ published = 0 /\ terminal = "none" /\ providerFinished = FALSE
        /\ dispatched = FALSE /\ taskDone = FALSE /\ lease = TRUE
        /\ cancelled = FALSE /\ closed = FALSE /\ terminalCount = 0
Dispatch == /\ ~dispatched /\ ~cancelled /\ ~taskDone
            /\ dispatched' = TRUE
            /\ UNCHANGED <<queue, produced, block, room, observed, assembled,
                 knownAtTerminal, published, terminal, providerFinished, taskDone,
                 lease, cancelled, closed, terminalCount>>
Emit(e) == /\ dispatched /\ ~taskDone /\ ~providerFinished /\ ~cancelled
           /\ produced < 4 /\ Len(queue) < 2
           /\ queue' = Append(queue, e) /\ produced' = produced + 1
           /\ observed' = IF Usage(e) # 0 THEN Usage(e) ELSE observed
           /\ providerFinished' = (e = "finish")
           /\ UNCHANGED <<block, room, assembled, knownAtTerminal, published,
                terminal, dispatched, taskDone, lease, cancelled, closed, terminalCount>>
Invalid(e) == \/ (e \in {"start", "tool"} /\ block # "none")
              \/ (e = "delta" /\ (block \notin {"none", "text"} \/ room = 0))
              \/ (e = "validTool" /\ block # "toolOpen")
              \/ (Usage(e) # 0 /\ room = 0)
              \/ (e = "finish" /\ block = "toolOpen")
Consume == /\ terminal = "none" /\ ~cancelled /\ Len(queue) > 0
           /\ LET e == Head(queue)
                  ending == Invalid(e) \/ e = "finish"
              IN /\ queue' = Tail(queue)
                 /\ IF ending
                    THEN /\ terminal' = IF Invalid(e) THEN "error"
                                        ELSE IF block = "toolComplete" THEN "tool" ELSE "stop"
                         /\ terminalCount' = terminalCount + 1
                         /\ knownAtTerminal' = observed
                         /\ published' = IF PreserveTerminalUsage
                                           THEN observed
                                           ELSE IF e = "finish" /\ room > 0 THEN observed ELSE assembled
                         /\ lease' = IF ReleaseOnTerminal THEN FALSE ELSE lease
                         /\ cancelled' = Invalid(e)
                         /\ UNCHANGED <<block, room, assembled>>
                    ELSE /\ block' = CASE e = "start" \/ e = "delta" -> "text"
                                          [] e = "tool" -> "toolOpen"
                                          [] e = "validTool" -> "toolComplete"
                                          [] OTHER -> block
                         /\ room' = IF e = "delta" THEN room - 1 ELSE room
                         /\ assembled' = IF Usage(e) # 0 THEN Usage(e) ELSE assembled
                         /\ UNCHANGED <<knownAtTerminal, published, terminal, lease, cancelled, terminalCount>>
           /\ UNCHANGED <<produced, observed, providerFinished, dispatched, taskDone, closed>>
Cancel == /\ ~cancelled /\ terminal = "none" /\ cancelled' = TRUE
          /\ UNCHANGED <<queue, produced, block, room, observed, assembled,
               knownAtTerminal, published, terminal, providerFinished, dispatched,
               taskDone, lease, closed, terminalCount>>
SyntheticFinish == /\ terminal = "none"
                   /\ (cancelled \/ (taskDone /\ Len(queue) = 0))
                   /\ terminal' = IF cancelled THEN "aborted" ELSE "error"
                   /\ terminalCount' = terminalCount + 1
                   /\ knownAtTerminal' = observed /\ published' = observed
                   /\ lease' = IF ReleaseOnTerminal THEN FALSE ELSE lease
                   /\ UNCHANGED <<queue, produced, block, room, observed, assembled,
                        providerFinished, dispatched, taskDone, cancelled, closed>>
TaskComplete == /\ ~taskDone /\ (dispatched \/ cancelled)
                /\ taskDone' = TRUE /\ lease' = FALSE
                /\ UNCHANGED <<queue, produced, block, room, observed, assembled,
                     knownAtTerminal, published, terminal, providerFinished,
                     dispatched, cancelled, closed, terminalCount>>
Close == /\ ~closed
         /\ closed' = taskDone
         /\ cancelled' = (cancelled \/ terminal = "none")
         /\ UNCHANGED <<queue, produced, block, room, observed, assembled,
              knownAtTerminal, published, terminal, providerFinished,
              dispatched, taskDone, lease, terminalCount>>
Next == Dispatch \/ (\E e \in Events : Emit(e)) \/ Consume \/ Cancel
        \/ SyntheticFinish \/ TaskComplete \/ Close
TypeOK == /\ queue \in Seq(Events) /\ Len(queue) <= 2 /\ produced \in 0..4
          /\ block \in {"none", "text", "toolOpen", "toolComplete"} /\ room \in 0..2
          /\ observed \in 0..2 /\ assembled \in 0..2 /\ knownAtTerminal \in 0..2
          /\ published \in 0..2 /\ terminal \in {"none", "stop", "tool", "error", "aborted"}
          /\ providerFinished \in BOOLEAN /\ dispatched \in BOOLEAN
          /\ taskDone \in BOOLEAN /\ lease \in BOOLEAN /\ cancelled \in BOOLEAN /\ closed \in BOOLEAN
          /\ terminalCount \in 0..2
NoLostKnownUsage == terminal # "none" => published = knownAtTerminal
NoPrematureRelease == ~taskDone => lease
NoFalseCleanup == closed => taskDone
NoExecutablePartialTool == terminal = "tool" => block = "toolComplete"
NoFabricatedUsage == terminal = "none" => published = 0
OneTerminal == terminalCount <= 1 /\ ((terminal = "none") = (terminalCount = 0))
Spec == Init /\ [][Next]_vars
=============================================================================
