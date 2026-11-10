--------------------------- MODULE BatchRecovery ---------------------------
EXTENDS Naturals
CONSTANTS AllowMutation, SkipAuthorization
VARIABLES authorized, visible, child, reserved, sends
vars == <<authorized, visible, child, reserved, sends>>
Init == /\ authorized \in BOOLEAN
        /\ visible = FALSE
        /\ child = "UNKNOWN"
        /\ reserved = 1
        /\ sends = 0
Read == /\ (authorized \/ SkipAuthorization)
        /\ visible' = TRUE
        /\ UNCHANGED <<authorized, child, reserved, sends>>
Reserve == /\ AllowMutation /\ reserved = 1
           /\ reserved' = 2
           /\ UNCHANGED <<authorized, visible, child, sends>>
Finish == /\ AllowMutation /\ child = "UNKNOWN"
          /\ child' = "COMPLETE"
          /\ UNCHANGED <<authorized, visible, reserved, sends>>
Send == /\ AllowMutation /\ sends = 0
        /\ sends' = 1
        /\ UNCHANGED <<authorized, visible, child, reserved>>
Next == Read \/ Reserve \/ Finish \/ Send
Spec == Init /\ [][Next]_vars
TypeOK == /\ authorized \in BOOLEAN /\ visible \in BOOLEAN
          /\ child \in {"UNKNOWN", "COMPLETE"}
          /\ reserved \in 1..2 /\ sends \in 0..1
RecoveryReadOnly == child = "UNKNOWN" /\ reserved = 1 /\ sends = 0
AuthorizedRead == visible => authorized
=============================================================================
