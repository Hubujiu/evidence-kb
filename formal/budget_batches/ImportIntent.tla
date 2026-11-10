-------------------------- MODULE ImportIntent --------------------------
EXTENDS Naturals
CONSTANTS SkipIntentSeal, DropImportMarker, DropUnknownRow
Destinations == 1..2
SourceRows == 1..2
VARIABLES occupied, intent, requested, phase, importedRows, markers
vars == <<occupied, intent, requested, phase, importedRows, markers>>
Init == /\ occupied \in [Destinations -> BOOLEAN] /\ intent = 0 /\ requested = 1 /\ phase = "idle"
        /\ importedRows = [d \in Destinations |-> {}]
        /\ markers = [d \in Destinations |-> FALSE]
Start(d) == /\ phase \in {"idle", "failed", "returned"} /\ requested' = d /\ phase' = "checked"
            /\ UNCHANGED <<occupied, intent, importedRows, markers>>
PinIntent == /\ phase = "checked" /\ intent \in {0, requested}
             /\ intent' = (IF SkipIntentSeal THEN intent ELSE requested) /\ phase' = "prepare"
             /\ UNCHANGED <<occupied, requested, importedRows, markers>>
RejectOther == /\ phase = "checked" /\ intent \notin {0, requested} /\ phase' = "failed"
               /\ UNCHANGED <<occupied, intent, requested, importedRows, markers>>
Prepare == /\ phase = "prepare"
           /\ phase' = IF markers[requested] THEN "returned"
                        ELSE IF occupied[requested] THEN "failed" ELSE "importing"
           /\ UNCHANGED <<occupied, intent, requested, importedRows, markers>>
Fail == /\ phase \in {"prepare", "importing", "written"} /\ phase' = "failed"
        /\ UNCHANGED <<occupied, intent, requested, importedRows, markers>>
Commit == /\ phase = "importing" /\ phase' = "written"
          /\ importedRows' = [importedRows EXCEPT ![requested] = IF DropUnknownRow THEN {1} ELSE SourceRows]
          /\ markers' = [markers EXCEPT ![requested] = ~DropImportMarker]
          /\ UNCHANGED <<occupied, intent, requested>>
Return == /\ phase = "written" /\ phase' = "returned"
          /\ UNCHANGED <<occupied, intent, requested, importedRows, markers>>
Next == (\E d \in Destinations : Start(d)) \/ PinIntent \/ RejectOther \/ Prepare \/ Fail \/ Commit \/ Return
Spec == Init /\ [][Next]_vars
TypeOK == /\ occupied \in [Destinations -> BOOLEAN] /\ intent \in 0..2 /\ requested \in Destinations
          /\ phase \in {"idle", "checked", "prepare", "importing", "written", "failed", "returned"}
          /\ importedRows \in [Destinations -> SUBSET SourceRows] /\ markers \in [Destinations -> BOOLEAN]
DestinationWorkFenced == phase \in {"prepare", "importing", "written", "returned"} => intent = requested
AtomicRowsAndMarker == \A d \in Destinations : (importedRows[d] # {}) = markers[d]
CompleteImportRetainsUnknown == \A d \in Destinations : markers[d] => importedRows[d] = SourceRows
NeverOverwriteOccupied == \A d \in Destinations : occupied[d] => importedRows[d] = {} /\ ~markers[d]
NoMigrationFork == \A d \in Destinations : importedRows[d] # {} => d = intent
=============================================================================
