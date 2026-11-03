--------------------------- MODULE StageCache ---------------------------
EXTENDS Naturals
CONSTANTS SkipAuthorization, SkipExactKey, SkipIntegrity
Keys == 1..2
Contents == 1..2
VARIABLES index, sealed, payload, authority, phase, requested, admitted, bypass,
          selected, expected, observed, delivered
vars == <<index, sealed, payload, authority, phase, requested, admitted, bypass,
          selected, expected, observed, delivered>>
Init == /\ index = [k \in Keys |-> 0] /\ sealed = index
        /\ payload = [v \in Contents |-> 0] /\ authority = TRUE /\ phase = "new"
        /\ requested = 1 /\ admitted = FALSE /\ bypass = FALSE
        /\ selected = 1 /\ expected = 0 /\ observed = 0 /\ delivered = FALSE
PutIndex(k, v) == /\ index[k] \in {0, v}
                  /\ index' = [index EXCEPT ![k] = v]
                  /\ sealed' = [sealed EXCEPT ![k] = IF @ = 0 THEN v ELSE @]
                  /\ UNCHANGED <<payload, authority, phase, requested, admitted, bypass,
                                 selected, expected, observed, delivered>>
Remember(v) == /\ v \in {index[k] : k \in Keys}
               /\ payload' = [payload EXCEPT ![v] = v]
               /\ UNCHANGED <<index, sealed, authority, phase, requested, admitted, bypass,
                              selected, expected, observed, delivered>>
Lose(v) == /\ payload[v] # 0 /\ payload' = [payload EXCEPT ![v] = 0]
           /\ UNCHANGED <<index, sealed, authority, phase, requested, admitted, bypass,
                          selected, expected, observed, delivered>>
Corrupt(v) == /\ payload[v] # 0 /\ payload' = [payload EXCEPT ![v] = 3]
              /\ UNCHANGED <<index, sealed, authority, phase, requested, admitted, bypass,
                             selected, expected, observed, delivered>>
Revoke == /\ authority /\ authority' = FALSE
          /\ UNCHANGED <<index, sealed, payload, phase, requested, admitted, bypass,
                         selected, expected, observed, delivered>>
BeginRead(k, forceExecute) ==
    /\ phase = "new" /\ requested' = k /\ admitted' = authority /\ bypass' = forceExecute
    /\ phase' = IF ~authority /\ ~SkipAuthorization THEN "denied"
                 ELSE IF forceExecute THEN "miss" ELSE "lookup"
    /\ UNCHANGED <<index, sealed, payload, authority, selected, expected, observed, delivered>>
Lookup == LET k == IF SkipExactKey /\ index[requested] = 0 THEN 3 - requested ELSE requested
          IN /\ phase = "lookup" /\ selected' = k /\ expected' = index[k]
             /\ phase' = IF index[k] = 0 THEN "miss" ELSE "read"
             /\ UNCHANGED <<index, sealed, payload, authority, requested, admitted, bypass, observed, delivered>>
ReadPayload == /\ phase = "read" /\ observed' = payload[expected]
               /\ delivered' = (payload[expected] # 0 /\ (SkipIntegrity \/ payload[expected] = expected))
               /\ phase' = "done"
               /\ UNCHANGED <<index, sealed, payload, authority, requested, admitted, bypass, selected, expected>>
Next == (\E k \in Keys, v \in Contents : PutIndex(k, v))
        \/ (\E v \in Contents : Remember(v) \/ Lose(v) \/ Corrupt(v))
        \/ (\E k \in Keys, forceExecute \in BOOLEAN : BeginRead(k, forceExecute))
        \/ Revoke \/ Lookup \/ ReadPayload
Spec == Init /\ [][Next]_vars
TypeOK == /\ index \in [Keys -> 0..2] /\ sealed \in [Keys -> 0..2] /\ payload \in [Contents -> 0..3]
          /\ authority \in BOOLEAN /\ admitted \in BOOLEAN /\ bypass \in BOOLEAN /\ delivered \in BOOLEAN
          /\ phase \in {"new", "denied", "miss", "lookup", "read", "done"}
          /\ requested \in Keys /\ selected \in Keys /\ expected \in 0..2 /\ observed \in 0..3
ImmutableBinding == index = sealed
AdmissionAuthorized == delivered => admitted /\ ~bypass
ExactDependencyKey == delivered => selected = requested /\ index[requested] = expected
ContentIntegrity == delivered => observed = expected /\ expected \in Contents
=============================================================================
