---------------------- MODULE ToolTemporaryIsolation ----------------------
EXTENDS Naturals
CONSTANT SharedNamespace
Workers == 1..2
Namespace(i) == IF SharedNamespace THEN 1 ELSE i
VARIABLES phase, files
vars == <<phase, files>>
Init == /\ phase = [i \in Workers |-> "new"]
        /\ files = [i \in Workers |-> FALSE]
Prepare(i) == /\ phase[i] = "new"
              /\ phase' = [phase EXCEPT ![i] = "prepared"]
              /\ files' = [files EXCEPT ![Namespace(i)] = TRUE]
Load(i) == /\ phase[i] = "prepared" /\ files[Namespace(i)]
           /\ phase' = [phase EXCEPT ![i] = "loaded"]
           /\ UNCHANGED files
Exit(i) == /\ phase[i] = "loaded"
           /\ phase' = [phase EXCEPT ![i] = "closed"]
           /\ files' = [files EXCEPT ![Namespace(i)] = FALSE]
Next == \E i \in Workers: Prepare(i) \/ Load(i) \/ Exit(i)
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in [Workers -> {"new", "prepared", "loaded", "closed"}]
          /\ files \in [Workers -> BOOLEAN]
ActiveFilesRetained == \A i \in Workers: phase[i] \in {"prepared", "loaded"} => files[Namespace(i)]
NoOverlapWitness == ~(phase[1] = "loaded" /\ phase[2] = "loaded")
NoSurvivorWitness == ~(phase[1] = "closed" /\ phase[2] = "loaded" /\ files[Namespace(2)])
=============================================================================
