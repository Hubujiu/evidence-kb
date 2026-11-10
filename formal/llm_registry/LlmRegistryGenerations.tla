----------------------- MODULE LlmRegistryGenerations -----------------------
EXTENDS Naturals, FiniteSets
CONSTANT SkipBindingCheck
VARIABLES route, phase, generation, nextGeneration, ticket, ticketRoute,
          ticketGeneration, adopted, replacement, destroyed
vars == <<route, phase, generation, nextGeneration, ticket, ticketRoute,
          ticketGeneration, adopted, replacement, destroyed>>
Registrations == 1..2
Tickets == 1..2
LiveCalls(r) == {i \in Tickets : ticketRoute[i] = r /\ ticket[i] \in {"admitted", "completed"}}
SnapshotRefs(r) == {i \in Tickets : ticketRoute[i] = r /\ ticket[i] \in {"prepared", "admitted", "completed"}}
Init == /\ route = 1 /\ phase = [r \in Registrations |-> IF r = 1 THEN "active" ELSE "absent"]
        /\ generation = [r \in Registrations |-> IF r = 1 THEN 1 ELSE 0]
        /\ nextGeneration = 1 /\ ticket = [i \in Tickets |-> "idle"]
        /\ ticketRoute = [i \in Tickets |-> 0] /\ ticketGeneration = [i \in Tickets |-> 0]
        /\ adopted = FALSE /\ replacement = FALSE /\ destroyed = [r \in Registrations |-> FALSE]
Prepare(i) == /\ ticket[i] = "idle" /\ route # 0
              /\ ticket' = [ticket EXCEPT ![i] = "prepared"]
              /\ ticketRoute' = [ticketRoute EXCEPT ![i] = route]
              /\ ticketGeneration' = [ticketGeneration EXCEPT ![i] = generation[route]]
              /\ UNCHANGED <<route, phase, generation, nextGeneration, adopted, replacement, destroyed>>
Admit(i) == /\ ticket[i] = "prepared"
            /\ LET r == ticketRoute[i]
                   current == route = r /\ phase[r] = "active" /\ generation[r] = ticketGeneration[i]
               IN ticket' = [ticket EXCEPT ![i] = IF current \/ SkipBindingCheck THEN "admitted" ELSE "rejected"]
            /\ UNCHANGED <<route, phase, generation, nextGeneration, ticketRoute,
                 ticketGeneration, adopted, replacement, destroyed>>
Complete(i) == /\ ticket[i] = "admitted" /\ ticket' = [ticket EXCEPT ![i] = "completed"]
               /\ UNCHANGED <<route, phase, generation, nextGeneration, ticketRoute,
                    ticketGeneration, adopted, replacement, destroyed>>
Release(i) == /\ ticket[i] \in {"admitted", "completed"}
              /\ ticket' = [ticket EXCEPT ![i] = "released"]
              /\ LET r == ticketRoute[i]
                 IN phase' = IF phase[r] = "detached" /\ LiveCalls(r) = {i}
                             THEN [phase EXCEPT ![r] = "absent"] ELSE phase
              /\ UNCHANGED <<route, generation, nextGeneration, ticketRoute,
                   ticketGeneration, adopted, replacement, destroyed>>
Discard(i) == /\ ticket[i] = "prepared" /\ ticket' = [ticket EXCEPT ![i] = "rejected"]
              /\ UNCHANGED <<route, phase, generation, nextGeneration, ticketRoute,
                   ticketGeneration, adopted, replacement, destroyed>>
Detach == /\ route # 0
          /\ phase' = [phase EXCEPT ![route] = IF LiveCalls(route) = {} THEN "absent" ELSE "detached"]
          /\ route' = 0
          /\ UNCHANGED <<generation, nextGeneration, ticket, ticketRoute,
               ticketGeneration, adopted, replacement, destroyed>>
Adopt == /\ ~adopted /\ route # 0 /\ nextGeneration < 3
         /\ \A r \in Registrations : LiveCalls(r) = {}
         /\ generation' = [generation EXCEPT ![route] = nextGeneration + 1]
         /\ nextGeneration' = nextGeneration + 1 /\ adopted' = TRUE
         /\ UNCHANGED <<route, phase, ticket, ticketRoute, ticketGeneration, replacement, destroyed>>
RegisterReplacement == /\ route = 0 /\ ~replacement /\ nextGeneration < 3
                       /\ route' = 2 /\ phase' = [phase EXCEPT ![2] = "active"]
                       /\ generation' = [generation EXCEPT ![2] = nextGeneration + 1]
                       /\ nextGeneration' = nextGeneration + 1 /\ replacement' = TRUE
                       /\ UNCHANGED <<ticket, ticketRoute, ticketGeneration, adopted, destroyed>>
Destroy(r) == /\ generation[r] # 0 /\ phase[r] = "absent" /\ SnapshotRefs(r) = {} /\ ~destroyed[r]
              /\ destroyed' = [destroyed EXCEPT ![r] = TRUE]
              /\ UNCHANGED <<route, phase, generation, nextGeneration, ticket, ticketRoute,
                   ticketGeneration, adopted, replacement>>
Next == (\E i \in Tickets : Prepare(i) \/ Admit(i) \/ Complete(i) \/ Release(i) \/ Discard(i))
        \/ Detach \/ Adopt \/ RegisterReplacement \/ (\E r \in Registrations : Destroy(r))
TypeOK == /\ route \in 0..2 /\ phase \in [Registrations -> {"active", "detached", "absent"}]
          /\ generation \in [Registrations -> 0..3] /\ nextGeneration \in 1..3
          /\ ticket \in [Tickets -> {"idle", "prepared", "admitted", "completed", "released", "rejected"}]
          /\ ticketRoute \in [Tickets -> 0..2] /\ ticketGeneration \in [Tickets -> 0..3]
          /\ adopted \in BOOLEAN /\ replacement \in BOOLEAN /\ destroyed \in [Registrations -> BOOLEAN]
PinnedAdmission == \A i \in Tickets : ticket[i] \in {"admitted", "completed"} =>
                   (ticketRoute[i] # 0 /\ phase[ticketRoute[i]] # "absent"
                    /\ ticketGeneration[i] = generation[ticketRoute[i]])
NoMixedGeneration == \A i \in Tickets : ticket[i] \in {"admitted", "completed"} => ticketGeneration[i] = generation[ticketRoute[i]]
RouteOwnsEntry == route # 0 => phase[route] = "active"
SnapshotOwnsPayload == \A r \in Registrations : (phase[r] # "absent" \/ SnapshotRefs(r) # {}) => ~destroyed[r]
DetachedHasAdmission == \A r \in Registrations : phase[r] = "detached" => LiveCalls(r) # {}
Spec == Init /\ [][Next]_vars
=============================================================================
