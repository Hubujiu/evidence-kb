-------------------------- MODULE PermissionLatch --------------------------
EXTENDS Naturals
CONSTANT BlockBeforeSettlement
VARIABLES phase, ledger, latched, durableBlock, observed403, sends, restarted
vars == <<phase, ledger, latched, durableBlock, observed403, sends, restarted>>
Init == /\ phase = "ready" /\ ledger = "none" /\ latched = FALSE
        /\ durableBlock = FALSE /\ observed403 = FALSE /\ sends = 0 /\ restarted = FALSE
Send == /\ phase = "ready" /\ ~latched /\ ledger # "STARTED" /\ sends < 2
        /\ sends' = sends + 1 /\ ledger' = "STARTED" /\ phase' = "waiting"
        /\ UNCHANGED <<latched, durableBlock, observed403, restarted>>
Receive403 == /\ phase = "waiting" /\ phase' = "observed" /\ observed403' = TRUE
              /\ UNCHANGED <<ledger, latched, durableBlock, sends, restarted>>
EarlyBlock(persisted) == /\ phase = "observed" /\ BlockBeforeSettlement /\ persisted \in BOOLEAN
                         /\ latched' = TRUE /\ durableBlock' = persisted
                         /\ phase' = (IF persisted THEN "settle" ELSE "returned")
                         /\ UNCHANGED <<ledger, observed403, sends, restarted>>
Settle(outcome) == /\ (phase = "settle" \/ (phase = "observed" /\ ~BlockBeforeSettlement))
                  /\ outcome \in {"committed", "commit-then-error", "rollback-error"}
                  /\ ledger' = (IF outcome = "rollback-error" THEN "STARTED" ELSE "ERROR")
                  /\ phase' = (IF BlockBeforeSettlement \/ outcome # "committed"
                               THEN "returned" ELSE "after-settle")
                  /\ UNCHANGED <<latched, durableBlock, observed403, sends, restarted>>
LateBlock(persisted) == /\ phase = "after-settle" /\ ~BlockBeforeSettlement /\ persisted \in BOOLEAN
                        /\ latched' = TRUE /\ durableBlock' = (durableBlock \/ persisted)
                        /\ phase' = "returned"
                        /\ UNCHANGED <<ledger, observed403, sends, restarted>>
NextInvocation == /\ phase = "returned" /\ phase' = "ready"
                  /\ UNCHANGED <<ledger, latched, durableBlock, observed403, sends, restarted>>
Restart == /\ phase = "returned" /\ ~restarted /\ restarted' = TRUE
           /\ latched' = durableBlock /\ phase' = "ready"
           /\ UNCHANGED <<ledger, durableBlock, observed403, sends>>
Next == Send \/ Receive403 \/ (\E persisted \in BOOLEAN : EarlyBlock(persisted) \/ LateBlock(persisted))
        \/ (\E outcome \in {"committed", "commit-then-error", "rollback-error"} : Settle(outcome))
        \/ NextInvocation \/ Restart
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"ready", "waiting", "observed", "settle", "after-settle", "returned"}
          /\ ledger \in {"none", "STARTED", "ERROR"}
          /\ latched \in BOOLEAN /\ durableBlock \in BOOLEAN /\ observed403 \in BOOLEAN
          /\ sends \in 0..2 /\ restarted \in BOOLEAN
NoUnacknowledgedResend == sends <= 1
Settled403RetainsBlock == (observed403 /\ ledger = "ERROR") => latched /\ durableBlock
BlockAndSettlementUnreachable == ~(observed403 /\ ledger = "ERROR" /\ durableBlock /\ phase = "returned")
=============================================================================
