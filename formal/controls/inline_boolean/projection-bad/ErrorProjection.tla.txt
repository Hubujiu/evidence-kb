------------------------ MODULE ErrorProjection ------------------------
EXTENDS Naturals
CONSTANTS DropGenericFacts, LeakGenericText, InventUnsent
Missing == [attempts |-> "absent", usage |-> "unknown", sent |-> "unknown"]
Facts == {Missing,
          [attempts |-> "zero", usage |-> "unknown", sent |-> "no"],
          [attempts |-> "one", usage |-> "unknown", sent |-> "unknown"],
          [attempts |-> "one", usage |-> "zero", sent |-> "yes"],
          [attempts |-> "one", usage |-> "one", sent |-> "yes"]}
Cost(facts) == IF facts.usage = "one" THEN 1 ELSE IF facts.usage = "zero" THEN 0
              ELSE IF facts.attempts = "one" THEN 1 ELSE 0
VARIABLES phase, kind, source, pending, ledger, wire, event,
          publicCode, publicMessage, privateVisible, originalRaised
vars == <<phase, kind, source, pending, ledger, wire, event,
          publicCode, publicMessage, privateVisible, originalRaised>>
Init == /\ phase = "new" /\ kind \in {"generic", "coded", "cancel"} /\ source \in Facts
        /\ pending = FALSE /\ ledger = 0 /\ wire = Missing /\ event = Missing
        /\ publicCode = "none" /\ publicMessage = "none"
        /\ privateVisible = FALSE /\ originalRaised = FALSE
Begin == /\ phase = "new" /\ phase' = "active" /\ pending' = TRUE
         /\ UNCHANGED <<kind, source, ledger, wire, event, publicCode, publicMessage, privateVisible, originalRaised>>
Fail == /\ phase = "active" /\ phase' = "failed" /\ pending' = FALSE /\ ledger' = Cost(source)
        /\ UNCHANGED <<kind, source, wire, event, publicCode, publicMessage, privateVisible, originalRaised>>
Project == /\ phase = "failed" /\ kind # "cancel" /\ phase' = "projected"
           /\ wire' = IF DropGenericFacts /\ kind = "generic" THEN Missing
                      ELSE IF InventUnsent /\ source.sent = "unknown" THEN [source EXCEPT !.sent = "no"]
                      ELSE source
           /\ publicCode' = IF kind = "generic" THEN "CONTRACT_IMPLEMENTATION_FAILED" ELSE "domain-code"
           /\ publicMessage' = IF kind = "generic" THEN
                  (IF LeakGenericText THEN "private-error-text" ELSE "Service implementation failed") ELSE "domain-message"
           /\ privateVisible' = LeakGenericText /\ kind = "generic"
           /\ UNCHANGED <<kind, source, pending, ledger, event, originalRaised>>
Observe == /\ phase = "projected" /\ phase' = "observed" /\ event' = wire
           /\ UNCHANGED <<kind, source, pending, ledger, wire, publicCode, publicMessage, privateVisible, originalRaised>>
Cancel == /\ phase = "failed" /\ kind = "cancel" /\ phase' = "cancelled" /\ originalRaised' = TRUE
          /\ UNCHANGED <<kind, source, pending, ledger, wire, event, publicCode, publicMessage, privateVisible>>
Next == Begin \/ Fail \/ Project \/ Observe \/ Cancel
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"new", "active", "failed", "projected", "observed", "cancelled"}
          /\ kind \in {"generic", "coded", "cancel"} /\ source \in Facts
          /\ pending \in BOOLEAN /\ ledger \in 0..1
          /\ wire \in [attempts : {"absent", "zero", "one"}, usage : {"unknown", "zero", "one"}, sent : {"unknown", "yes", "no"}]
          /\ event \in [attempts : {"absent", "zero", "one"}, usage : {"unknown", "zero", "one"}, sent : {"unknown", "yes", "no"}]
          /\ publicCode \in {"none", "CONTRACT_IMPLEMENTATION_FAILED", "domain-code"}
          /\ publicMessage \in {"none", "Service implementation failed", "private-error-text", "domain-message"}
          /\ privateVisible \in BOOLEAN /\ originalRaised \in BOOLEAN
ObservedFactsRetained == phase = "observed" => event = source
GenericDiagnosticsSafe == kind = "generic" /\ phase \in {"projected", "observed"} =>
                         publicCode = "CONTRACT_IMPLEMENTATION_FAILED" /\ publicMessage = "Service implementation failed"
                         /\ ~privateVisible
UnknownStaysUnknown == phase \in {"projected", "observed"} /\ source.sent = "unknown" => wire.sent = "unknown"
LedgerCostRetained == phase \in {"failed", "projected", "observed", "cancelled"} => ledger = Cost(source)
FailedSlotReleased == phase \in {"failed", "projected", "observed", "cancelled"} => ~pending
CancellationIdentity == phase = "cancelled" => originalRaised /\ publicCode = "none" /\ wire = Missing
=============================================================================
