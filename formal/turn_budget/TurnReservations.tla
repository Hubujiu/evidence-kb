------------------------ MODULE TurnReservations ------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS DropTurnRollback, DropObservedUsage, CountCachedAttempt, SkipGlobalReservation, RefundUnobserved
Calls == 1..3
Turns == 1..2
MaxAttempts == [c \in Calls |-> IF c = 1 THEN 2 ELSE 1]
Turn(c) == IF c = 1 THEN 1 ELSE 2
GlobalCallCap == 3
TurnCallCap == 2
TokenCap == 6
PerAttempt == 2
RECURSIVE SumFor(_, _)
SumFor(domain, values) == IF domain = {} THEN 0
                         ELSE LET c == CHOOSE x \in domain : TRUE
                              IN values[c] + SumFor(domain \ {c}, values)
VARIABLES phase, pendingGlobal, pendingTurn, settledGlobal, settledTurn,
          attempts, observed, reported, failed, reservedAtSend,
          globalKnown, globalUnknown, turnKnown, turnUnknown
vars == <<phase, pendingGlobal, pendingTurn, settledGlobal, settledTurn,
          attempts, observed, reported, failed, reservedAtSend,
          globalKnown, globalUnknown, turnKnown, turnUnknown>>
ForTurn(t) == {c \in Calls : Turn(c) = t}
GlobalCalls == SumFor(settledGlobal, attempts)
TurnCalls(t) == SumFor(settledTurn \cap ForTurn(t), attempts)
GlobalCost == SumFor(Calls, globalKnown) + PerAttempt * SumFor(Calls, globalUnknown)
TurnCost(t) == SumFor(ForTurn(t), turnKnown) + PerAttempt * SumFor(ForTurn(t), turnUnknown)
GlobalAllows(c) == /\ GlobalCalls + SumFor(pendingGlobal, MaxAttempts) + MaxAttempts[c] <= GlobalCallCap
                   /\ GlobalCost + PerAttempt * (SumFor(pendingGlobal, MaxAttempts) + MaxAttempts[c]) <= TokenCap
TurnAllows(c) == /\ TurnCalls(Turn(c)) + SumFor(pendingTurn \cap ForTurn(Turn(c)), MaxAttempts) + MaxAttempts[c] <= TurnCallCap
                 /\ TurnCost(Turn(c)) + PerAttempt * (SumFor(pendingTurn \cap ForTurn(Turn(c)), MaxAttempts) + MaxAttempts[c]) <= TokenCap
KeepKnown(c) == observed[c] /\ ~(DropObservedUsage /\ failed[c])
KnownAmount(c) == IF KeepKnown(c) THEN reported[c] ELSE 0
UnknownAttempts(c) == IF RefundUnobserved /\ ~observed[c] THEN 0
                      ELSE attempts[c] - (IF KeepKnown(c) THEN 1 ELSE 0)
Init == /\ phase = [c \in Calls |-> "new"]
        /\ pendingGlobal = {} /\ pendingTurn = {} /\ settledGlobal = {} /\ settledTurn = {}
        /\ attempts = [c \in Calls |-> 0] /\ reported = [c \in Calls |-> 0]
        /\ observed = [c \in Calls |-> FALSE] /\ failed = [c \in Calls |-> FALSE]
        /\ reservedAtSend = [c \in Calls |-> FALSE]
        /\ globalKnown = [c \in Calls |-> 0] /\ globalUnknown = [c \in Calls |-> 0]
        /\ turnKnown = [c \in Calls |-> 0] /\ turnUnknown = [c \in Calls |-> 0]
Cache(c) == /\ phase[c] = "new" /\ phase' = [phase EXCEPT ![c] = "cached"]
            /\ attempts' = [attempts EXCEPT ![c] = IF CountCachedAttempt THEN 1 ELSE 0]
            /\ UNCHANGED <<pendingGlobal, pendingTurn, settledGlobal, settledTurn, observed, reported,
                           failed, reservedAtSend, globalKnown, globalUnknown, turnKnown, turnUnknown>>
ReserveTurn(c) == /\ phase[c] = "new" /\ TurnAllows(c)
                  /\ pendingTurn' = pendingTurn \cup {c} /\ phase' = [phase EXCEPT ![c] = "turn-held"]
                  /\ UNCHANGED <<pendingGlobal, settledGlobal, settledTurn, attempts, observed, reported,
                                 failed, reservedAtSend, globalKnown, globalUnknown, turnKnown, turnUnknown>>
DenyTurn(c) == /\ phase[c] = "new" /\ ~TurnAllows(c) /\ phase' = [phase EXCEPT ![c] = "denied"]
               /\ UNCHANGED <<pendingGlobal, pendingTurn, settledGlobal, settledTurn, attempts, observed, reported,
                              failed, reservedAtSend, globalKnown, globalUnknown, turnKnown, turnUnknown>>
ReserveGlobal(c) == /\ phase[c] = "turn-held" /\ (SkipGlobalReservation \/ GlobalAllows(c))
                    /\ pendingGlobal' = IF SkipGlobalReservation THEN pendingGlobal ELSE pendingGlobal \cup {c}
                    /\ phase' = [phase EXCEPT ![c] = "active"]
                    /\ UNCHANGED <<pendingTurn, settledGlobal, settledTurn, attempts, observed, reported,
                                   failed, reservedAtSend, globalKnown, globalUnknown, turnKnown, turnUnknown>>
DenyGlobal(c) == /\ phase[c] = "turn-held" /\ ~GlobalAllows(c)
                 /\ pendingTurn' = IF DropTurnRollback THEN pendingTurn ELSE pendingTurn \ {c}
                 /\ phase' = [phase EXCEPT ![c] = "denied"]
                 /\ UNCHANGED <<pendingGlobal, settledGlobal, settledTurn, attempts, observed, reported,
                                failed, reservedAtSend, globalKnown, globalUnknown, turnKnown, turnUnknown>>
Attempt(c) == /\ phase[c] = "active" /\ attempts[c] < MaxAttempts[c]
              /\ attempts' = [attempts EXCEPT ![c] = @ + 1]
              /\ reservedAtSend' = [reservedAtSend EXCEPT ![c] = c \in pendingGlobal \cap pendingTurn]
              /\ UNCHANGED <<phase, pendingGlobal, pendingTurn, settledGlobal, settledTurn, observed, reported,
                             failed, globalKnown, globalUnknown, turnKnown, turnUnknown>>
Unsent(c) == /\ phase[c] = "active" /\ attempts[c] = 0
             /\ phase' = [phase EXCEPT ![c] = "ended"] /\ failed' = [failed EXCEPT ![c] = TRUE]
             /\ UNCHANGED <<pendingGlobal, pendingTurn, settledGlobal, settledTurn, attempts, observed, reported,
                            reservedAtSend, globalKnown, globalUnknown, turnKnown, turnUnknown>>
Result(c, available, amount, failure) ==
    /\ phase[c] = "active" /\ attempts[c] > 0 /\ (available \/ amount = 0)
    /\ phase' = [phase EXCEPT ![c] = "ended"] /\ observed' = [observed EXCEPT ![c] = available]
    /\ reported' = [reported EXCEPT ![c] = amount] /\ failed' = [failed EXCEPT ![c] = failure]
    /\ UNCHANGED <<pendingGlobal, pendingTurn, settledGlobal, settledTurn, attempts, reservedAtSend,
                   globalKnown, globalUnknown, turnKnown, turnUnknown>>
SettleGlobal(c) == /\ phase[c] = "ended" /\ pendingGlobal' = pendingGlobal \ {c}
                   /\ settledGlobal' = settledGlobal \cup {c} /\ phase' = [phase EXCEPT ![c] = "global-settled"]
                   /\ globalKnown' = [globalKnown EXCEPT ![c] = KnownAmount(c)]
                   /\ globalUnknown' = [globalUnknown EXCEPT ![c] = UnknownAttempts(c)]
                   /\ UNCHANGED <<pendingTurn, settledTurn, attempts, observed, reported, failed,
                                  reservedAtSend, turnKnown, turnUnknown>>
SettleTurn(c) == /\ phase[c] = "global-settled" /\ pendingTurn' = pendingTurn \ {c}
                 /\ settledTurn' = settledTurn \cup {c} /\ phase' = [phase EXCEPT ![c] = "done"]
                 /\ turnKnown' = [turnKnown EXCEPT ![c] = KnownAmount(c)]
                 /\ turnUnknown' = [turnUnknown EXCEPT ![c] = UnknownAttempts(c)]
                 /\ UNCHANGED <<pendingGlobal, settledGlobal, attempts, observed, reported, failed,
                                reservedAtSend, globalKnown, globalUnknown>>
Next == \E c \in Calls : Cache(c) \/ ReserveTurn(c) \/ DenyTurn(c) \/ ReserveGlobal(c) \/ DenyGlobal(c)
         \/ Attempt(c) \/ Unsent(c) \/ SettleGlobal(c) \/ SettleTurn(c)
         \/ (\E available, failure \in BOOLEAN, amount \in {0, 3} : Result(c, available, amount, failure))
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in [Calls -> {"new", "cached", "turn-held", "active", "ended", "global-settled", "done", "denied"}]
          /\ pendingGlobal \subseteq Calls /\ pendingTurn \subseteq Calls /\ settledGlobal \subseteq Calls /\ settledTurn \subseteq Calls
          /\ attempts \in [Calls -> 0..2] /\ reported \in [Calls -> {0, 3}]
          /\ observed \in [Calls -> BOOLEAN] /\ failed \in [Calls -> BOOLEAN] /\ reservedAtSend \in [Calls -> BOOLEAN]
          /\ globalKnown \in [Calls -> {0, 3}] /\ turnKnown \in [Calls -> {0, 3}]
          /\ globalUnknown \in [Calls -> 0..2] /\ turnUnknown \in [Calls -> 0..2]
NoUnreservedSend == \A c \in Calls : attempts[c] > 0 => reservedAtSend[c]
TerminalOwnsNoReservation == \A c \in Calls : phase[c] \in {"cached", "denied", "done"} => c \notin pendingGlobal \cup pendingTurn
ObservedUsageRetained == \A c \in settledGlobal : observed[c] => globalKnown[c] = reported[c] /\ globalUnknown[c] = attempts[c] - 1
UnknownAttemptsRetained == \A c \in settledGlobal : ~observed[c] => globalKnown[c] = 0 /\ globalUnknown[c] = attempts[c]
CompletedLedgersAgree == \A c \in settledTurn : c \in settledGlobal /\ globalKnown[c] = turnKnown[c] /\ globalUnknown[c] = turnUnknown[c]
CacheHasNoPhysicalCost == \A c \in Calls : phase[c] = "cached" => attempts[c] = 0 /\ c \notin settledGlobal \cup settledTurn
                         /\ globalKnown[c] = 0 /\ globalUnknown[c] = 0 /\ turnKnown[c] = 0 /\ turnUnknown[c] = 0
GlobalCallsBound == GlobalCalls + SumFor(pendingGlobal, MaxAttempts) <= GlobalCallCap
TurnCallsBound == \A t \in Turns : TurnCalls(t) + SumFor(pendingTurn \cap ForTurn(t), MaxAttempts) <= TurnCallCap
=============================================================================
