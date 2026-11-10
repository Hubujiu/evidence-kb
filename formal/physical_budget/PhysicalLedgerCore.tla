-------------------------- MODULE PhysicalLedgerCore --------------------------
EXTENDS Integers, FiniteSets

CONSTANTS MissingAsZero, IgnoreCap, RewriteTerminal, IgnoreSettled
Requests == 1..2
Clients == 1..2
Resources == {"input", "output"}
States == {"absent", "STARTED", "COMPLETE", "ERROR", "UNKNOWN"}
Terminal == {"COMPLETE", "ERROR", "UNKNOWN"}
Cap == [k \in Resources |-> 2]
Reservation == [k \in Resources |-> 1]
UsageVectors == [Resources -> {-1, 0, 1, 3}]
Missing == [k \in Resources |-> -1]
Zero == [k \in Resources |-> 0]

VARIABLES rows, usage, winners, firstStatus, firstUsage, finishedClients,
          reservationChecked, reservationAccepted, requireSettled,
          priorPresent, priorUnresolved, priorTotal
vars == <<rows, usage, winners, firstStatus, firstUsage, finishedClients,
          reservationChecked, reservationAccepted, requireSettled,
          priorPresent, priorUnresolved, priorTotal>>

ExpectedCharge(r, k) == IF rows[r] = "absent" THEN 0
                        ELSE IF usage[r][k] = -1 THEN Reservation[k] ELSE usage[r][k]
Charge(r, k) == IF rows[r] = "absent" THEN 0
               ELSE IF usage[r][k] = -1 THEN (IF MissingAsZero THEN 0 ELSE Reservation[k])
               ELSE usage[r][k]
Total == [k \in Resources |-> Charge(1, k) + Charge(2, k)]
ExpectedTotal == [k \in Resources |-> ExpectedCharge(1, k) + ExpectedCharge(2, k)]
Unresolved == \E r \in Requests : rows[r] \in {"STARTED", "UNKNOWN"}

Init ==
    /\ rows = [r \in Requests |-> "absent"]
    /\ usage = [r \in Requests |-> Missing]
    /\ winners = [r \in Requests |-> 0]
    /\ firstStatus = [r \in Requests |-> "absent"]
    /\ firstUsage = [r \in Requests |-> Missing]
    /\ finishedClients = {}
    /\ reservationChecked = FALSE /\ reservationAccepted = FALSE
    /\ requireSettled = FALSE /\ priorPresent = FALSE /\ priorUnresolved = FALSE
    /\ priorTotal = Zero

Reserve(r, strict) ==
    LET allowed == rows[r] = "absent"
                   /\ (IgnoreSettled \/ ~strict \/ ~Unresolved)
                   /\ (IgnoreCap \/ \A k \in Resources : Total[k] + Reservation[k] <= Cap[k])
    IN /\ reservationChecked' = TRUE /\ reservationAccepted' = allowed
       /\ requireSettled' = strict /\ priorPresent' = (rows[r] # "absent")
       /\ priorUnresolved' = Unresolved /\ priorTotal' = ExpectedTotal
       /\ rows' = IF allowed THEN [rows EXCEPT ![r] = "STARTED"] ELSE rows
       /\ UNCHANGED <<usage, winners, firstStatus, firstUsage, finishedClients>>

Finish(c, r, state, observed) ==
    LET allowed == rows[r] = "STARTED" \/ (RewriteTerminal /\ rows[r] \in Terminal)
    IN /\ c \notin finishedClients
       /\ finishedClients' = finishedClients \cup {c}
       /\ rows' = IF allowed THEN [rows EXCEPT ![r] = state] ELSE rows
       /\ usage' = IF allowed THEN [usage EXCEPT ![r] = observed] ELSE usage
       /\ winners' = IF allowed THEN [winners EXCEPT ![r] = @ + 1] ELSE winners
       /\ firstStatus' = IF allowed /\ winners[r] = 0 THEN [firstStatus EXCEPT ![r] = state]
                         ELSE firstStatus
       /\ firstUsage' = IF allowed /\ winners[r] = 0 THEN [firstUsage EXCEPT ![r] = observed]
                        ELSE firstUsage
       /\ reservationChecked' = FALSE
       /\ UNCHANGED <<reservationAccepted, requireSettled, priorPresent, priorUnresolved, priorTotal>>

Next == (\E r \in Requests, strict \in BOOLEAN : Reserve(r, strict))
        \/ (\E c \in Clients, r \in Requests, state \in Terminal, observed \in UsageVectors :
                Finish(c, r, state, observed))
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ rows \in [Requests -> States] /\ usage \in [Requests -> UsageVectors]
    /\ winners \in [Requests -> 0..2] /\ firstStatus \in [Requests -> States]
    /\ firstUsage \in [Requests -> UsageVectors] /\ finishedClients \subseteq Clients
    /\ reservationChecked \in BOOLEAN /\ reservationAccepted \in BOOLEAN
    /\ requireSettled \in BOOLEAN /\ priorPresent \in BOOLEAN /\ priorUnresolved \in BOOLEAN
    /\ priorTotal \in [Resources -> 0..6]
DimensionFallbackExact == Total = ExpectedTotal
TerminalImmutable == \A r \in Requests :
    /\ winners[r] <= 1
    /\ (winners[r] = 1 => rows[r] = firstStatus[r] /\ usage[r] = firstUsage[r])
AdmissionBudgetSafe == reservationChecked /\ reservationAccepted =>
    \A k \in Resources : priorTotal[k] + Reservation[k] <= Cap[k]
SettledPolicyRespected == reservationChecked /\ reservationAccepted /\ requireSettled => ~priorUnresolved
AdmissionDecisionExact == reservationChecked =>
    (reservationAccepted = (~priorPresent /\ (~requireSettled \/ ~priorUnresolved)
        /\ \A k \in Resources : priorTotal[k] + Reservation[k] <= Cap[k]))
NoUsageClipping == \A r \in Requests : winners[r] = 1 => usage[r] = firstUsage[r]
=============================================================================
