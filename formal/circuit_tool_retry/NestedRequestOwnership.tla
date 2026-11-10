---------------------- MODULE NestedRequestOwnership ----------------------
EXTENDS Naturals, FiniteSets

CONSTANTS RetryUnknown, SkipPermit, DropUnknownCharge, ReplayTerminal
Ids == 1..4
VARIABLES pc, readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, opened, frozen, badSend, replayed
vars == <<pc, readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, opened, frozen, badSend, replayed>>

Init ==
    /\ pc = "ready" /\ readOnly \in BOOLEAN
    /\ outer = 0 /\ inner = 0 /\ physical = 0
    /\ acquired = 0 /\ settled = 0 /\ permit = FALSE
    /\ receipt = [i \in Ids |-> "absent"]
    /\ charge = [i \in Ids |-> 0]
    /\ outcome = "none" /\ opened = FALSE
    /\ frozen = <<0, 0, 0>> /\ badSend = FALSE /\ replayed = FALSE

Callback ==
    /\ pc = "ready" /\ outer < 2
    /\ outer' = outer + 1 /\ inner' = 0 /\ pc' = "acquire"
    /\ UNCHANGED <<readOnly, physical, acquired, settled, permit, receipt,
          charge, outcome, opened, frozen, badSend, replayed>>

Acquire ==
    /\ pc = "acquire" /\ ~opened
    /\ acquired' = acquired + 1 /\ permit' = TRUE /\ pc' = "reserve"
    /\ UNCHANGED <<readOnly, outer, inner, physical, settled, receipt,
          charge, outcome, opened, frozen, badSend, replayed>>

Refuse ==
    /\ pc = "acquire" /\ opened
    /\ outcome' = "admission" /\ pc' = "assess"
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, opened, frozen, badSend, replayed>>

Bypass ==
    /\ SkipPermit /\ pc = "acquire" /\ pc' = "reserve"
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, opened, frozen, badSend, replayed>>

Reserve ==
    /\ pc = "reserve" /\ physical < 4 /\ inner < 2
    /\ physical' = physical + 1 /\ inner' = inner + 1
    /\ receipt' = [receipt EXCEPT ![physical + 1] = "STARTED"]
    /\ charge' = [charge EXCEPT ![physical + 1] = 1]
    /\ pc' = "send"
    /\ UNCHANGED <<readOnly, outer, acquired, settled, permit, outcome,
          opened, frozen, badSend, replayed>>

Observe(value) ==
    /\ pc = "send"
    /\ receipt' = [receipt EXCEPT ![physical] =
          CASE value = "success" -> "COMPLETE"
            [] value = "unknown" -> "UNKNOWN" [] OTHER -> "ERROR"]
    /\ charge' = [charge EXCEPT ![physical] =
          IF value = "unknown" /\ DropUnknownCharge THEN 0 ELSE 1]
    /\ outcome' = value /\ badSend' = (badSend \/ ~permit)
    /\ pc' = "observed"
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, settled, permit,
          opened, frozen, replayed>>

InnerRetry ==
    /\ pc = "observed" /\ inner < 2
    /\ outcome = "transient" \/ (RetryUnknown /\ outcome = "unknown")
    /\ pc' = "reserve"
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, opened, frozen, badSend, replayed>>

SettlePermit ==
    /\ pc = "observed" /\ permit
    /\ settled' = settled + 1 /\ permit' = FALSE /\ pc' = "assess"
    /\ opened' = (outcome \in {"transient", "unknown"})
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, receipt, charge,
          outcome, frozen, badSend, replayed>>

OuterRetry ==
    /\ pc = "assess" /\ readOnly /\ outer < 2
    /\ outcome = "transient" \/ (RetryUnknown /\ outcome = "unknown")
    /\ pc' = "ready"
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, opened, frozen, badSend, replayed>>

Cooldown ==
    /\ opened /\ pc \in {"ready", "acquire", "assess"}
    /\ opened' = FALSE
    /\ UNCHANGED <<pc, readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, frozen, badSend, replayed>>

Finish ==
    /\ pc = "assess" /\ pc' = "terminal"
    /\ frozen' = <<outer, acquired, physical>>
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, opened, badSend, replayed>>

Reuse ==
    /\ pc = "terminal" /\ ~replayed
    /\ replayed' = TRUE /\ pc' = IF ReplayTerminal THEN "ready" ELSE pc
    /\ UNCHANGED <<readOnly, outer, inner, physical, acquired, settled, permit,
          receipt, charge, outcome, opened, frozen, badSend>>

Next == Callback \/ Acquire \/ Refuse \/ Bypass \/ Reserve
        \/ (\E value \in {"success", "transient", "unknown"}: Observe(value))
        \/ InnerRetry \/ SettlePermit \/ OuterRetry \/ Cooldown \/ Finish \/ Reuse
Spec == Init /\ [][Next]_vars
TypeOK == /\ pc \in {"ready", "acquire", "reserve", "send", "observed", "assess", "terminal"}
          /\ outer \in 0..2 /\ inner \in 0..2 /\ physical \in 0..4
          /\ acquired \in 0..2 /\ settled \in 0..2
          /\ permit \in BOOLEAN /\ readOnly \in BOOLEAN /\ opened \in BOOLEAN
          /\ receipt \in [Ids -> {"absent", "STARTED", "COMPLETE", "ERROR", "UNKNOWN"}]
          /\ charge \in [Ids -> 0..1] /\ badSend \in BOOLEAN /\ replayed \in BOOLEAN
PermitBalance == acquired = settled + (IF permit THEN 1 ELSE 0)
PhysicalNeedsPermit == ~badSend
NoUnknownReplay == \A i \in Ids: receipt[i] = "UNKNOWN" => physical = i
UnknownRetainsCharge == \A i \in Ids: receipt[i] \in {"STARTED", "UNKNOWN"} => charge[i] = 1
NoMutableOuterReplay == ~readOnly => outer <= 1
TerminalNoWork == replayed => <<outer, acquired, physical>> = frozen
PhysicalBound == physical <= 2 * acquired
NoInnerRetryWitness == ~(inner = 2 /\ acquired = 1 /\ physical = 2)
NoOuterRetryWitness == ~(outer = 2 /\ acquired = 2)
NoUnknownWitness == ~(pc = "terminal" /\ outcome = "unknown" /\ charge[physical] = 1)
=============================================================================
