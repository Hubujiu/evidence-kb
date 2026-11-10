--------------------------- MODULE StorageGrants ---------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS CheckGrant, CheckEntry
VARIABLES current, phase, binding, captured, entered, active, invoked
vars == <<current, phase, binding, captured, entered, active, invoked>>
Init == /\ current = 1 /\ phase = "idle" /\ binding = 0
        /\ captured = 0 /\ entered = 0 /\ active = 0 /\ invoked = 0
(* Two owners intentionally reuse the same route name and public registration.
   binding=0 is a direct registry lease; other values are issued grant owners.
   Replacement revokes all old grants. Ownership callbacks run outside lock. *)
Start == /\ phase = "idle"
         /\ binding' \in {0, 1, current}
         /\ phase' = "capture"
         /\ UNCHANGED <<current, captured, entered, active, invoked>>
ValidGrant == binding = 0 \/ binding = current
Capture == /\ phase = "capture"
           /\ (~CheckGrant \/ ValidGrant)
           /\ captured' = current /\ phase' = "ownership"
           /\ UNCHANGED <<current, binding, entered, active, invoked>>
RejectCapture == /\ phase = "capture" /\ CheckGrant /\ ~ValidGrant
                 /\ phase' = "rejected"
                 /\ UNCHANGED <<current, binding, captured, entered, active, invoked>>
Replace == /\ current = 1 /\ active = 0
           /\ current' = 2
           /\ UNCHANGED <<phase, binding, captured, entered, active, invoked>>
Admissible == (~CheckEntry \/ current = captured) /\ (~CheckGrant \/ ValidGrant)
Admit == /\ phase = "ownership" /\ Admissible
         /\ entered' = current /\ active' = 1 /\ phase' = "admitted"
         /\ UNCHANGED <<current, binding, captured, invoked>>
RejectRecheck == /\ phase = "ownership" /\ ~Admissible
                 /\ phase' = "rejected"
                 /\ UNCHANGED <<current, binding, captured, entered, active, invoked>>
Invoke == /\ phase = "admitted"
          /\ invoked' = IF binding = 0 THEN entered ELSE binding
          /\ phase' = "invoked"
          /\ UNCHANGED <<current, binding, captured, entered, active>>
Release == /\ phase = "invoked" /\ phase' = "done" /\ active' = 0
           /\ UNCHANGED <<current, binding, captured, entered, invoked>>
Next == Start \/ Capture \/ RejectCapture \/ Replace \/ Admit \/ RejectRecheck \/ Invoke \/ Release
Spec == Init /\ [][Next]_vars
TypeOK == /\ current \in {1, 2} /\ binding \in 0..2
          /\ captured \in 0..2 /\ entered \in 0..2 /\ invoked \in 0..2
          /\ active \in 0..1
          /\ phase \in {"idle", "capture", "ownership", "admitted", "invoked", "done", "rejected"}
OwnerContextMatches == active = 1 => captured = current
RevokedGrantCannotRun == active = 1 => (binding = 0 \/ binding = current)
ImplementationMatches == phase = "invoked" => invoked = entered
RejectedHasNoEffects == phase = "rejected" => (active = 0 /\ invoked = 0)
NoReplacementSuccess == ~(phase = "done" /\ current = 2 /\ binding = 2 /\ invoked = 2)
NoOldGrantRejected == ~(phase = "rejected" /\ current = 2 /\ binding = 1)
NoCallbackSwapRejected == ~(phase = "rejected" /\ current = 2 /\ binding = 0 /\ captured = 1)
=============================================================================
