-------------------------- MODULE ProviderAdmission --------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS Events, SeedEvent, BindEphemeralCheckpoint, RequireDurableAck,
          AckObservedOnly, FailClosedUnavailable, ValidateDurableHistory, LegacyOrphan
VARIABLES durable, ephemeral, seen, acknowledged, createdAcknowledgements,
          approvedEvents, storageAvailable, phase, observedEvents, ackSnapshot,
          expectedCheckpoint, restarted, sends, unsafeSend, sentAfterAck,
          probes, budgetRows, physicalErrorRows
vars == <<durable, ephemeral, seen, acknowledged, createdAcknowledgements,
          approvedEvents, storageAvailable, phase, observedEvents, ackSnapshot,
          expectedCheckpoint, restarted, sends, unsafeSend, sentAfterAck,
          probes, budgetRows, physicalErrorRows>>
Known == durable \cup ephemeral
ValidHistory == IF ValidateDurableHistory THEN acknowledged \subseteq durable
                ELSE acknowledged \subseteq Known
Readable == storageAvailable /\ ValidHistory
Checkpoint == <<durable, acknowledged,
                IF BindEphemeralCheckpoint THEN Known ELSE durable>>
Permit == (~FailClosedUnavailable /\ ~storageAvailable)
          \/ (Readable /\ Known \subseteq acknowledged)
Init == /\ durable = {}
        /\ ephemeral = (IF LegacyOrphan THEN {SeedEvent} ELSE {})
        /\ seen = (IF LegacyOrphan THEN {SeedEvent} ELSE {})
        /\ acknowledged = (IF LegacyOrphan THEN {SeedEvent} ELSE {})
        /\ createdAcknowledgements = {} /\ approvedEvents = {}
        /\ storageAvailable = TRUE /\ phase = "idle"
        /\ observedEvents = {} /\ ackSnapshot = {} /\ expectedCheckpoint = <<{}, {}, {}>>
        /\ restarted = FALSE /\ sends = 0 /\ unsafeSend = FALSE /\ sentAfterAck = FALSE
        /\ probes = 0 /\ budgetRows = "unchanged" /\ physicalErrorRows = "unchanged"
Observe403(event) == /\ event \in Events \ seen
                     /\ ephemeral' = ephemeral \cup {event} /\ seen' = seen \cup {event}
                     /\ UNCHANGED <<durable, acknowledged, createdAcknowledgements, approvedEvents,
                                    storageAvailable, phase, observedEvents, ackSnapshot, expectedCheckpoint,
                                    restarted, sends, unsafeSend, sentAfterAck, probes, budgetRows, physicalErrorRows>>
PersistEvent(event) == /\ event \in ephemeral \ durable /\ storageAvailable
                       /\ durable' = durable \cup {event}
                       /\ UNCHANGED <<ephemeral, seen, acknowledged, createdAcknowledgements, approvedEvents,
                                      storageAvailable, phase, observedEvents, ackSnapshot, expectedCheckpoint,
                                      restarted, sends, unsafeSend, sentAfterAck, probes, budgetRows, physicalErrorRows>>
StorageChange == /\ storageAvailable' = ~storageAvailable
                 /\ UNCHANGED <<durable, ephemeral, seen, acknowledged, createdAcknowledgements, approvedEvents,
                                phase, observedEvents, ackSnapshot, expectedCheckpoint, restarted,
                                sends, unsafeSend, sentAfterAck, probes, budgetRows, physicalErrorRows>>
Inspect == /\ phase = "idle" /\ Readable /\ Known # {}
           /\ phase' = "inspected" /\ observedEvents' = Known /\ expectedCheckpoint' = Checkpoint
           /\ UNCHANGED <<durable, ephemeral, seen, acknowledged, createdAcknowledgements, approvedEvents,
                          storageAvailable, ackSnapshot, restarted, sends, unsafeSend, sentAfterAck,
                          probes, budgetRows, physicalErrorRows>>
AckRead == /\ phase = "inspected"
           /\ LET valid == Readable /\ expectedCheckpoint = Checkpoint
                            /\ Known \ acknowledged # {}
                            /\ (~RequireDurableAck \/ Known \subseteq durable)
              IN /\ phase' = (IF valid THEN "prepared" ELSE "rejected")
                 /\ ackSnapshot' = (IF valid THEN Known ELSE ackSnapshot)
                 /\ approvedEvents' = (IF valid THEN approvedEvents \cup observedEvents ELSE approvedEvents)
           /\ UNCHANGED <<durable, ephemeral, seen, acknowledged, createdAcknowledgements, storageAvailable,
                          observedEvents, expectedCheckpoint, restarted, sends, unsafeSend, sentAfterAck,
                          probes, budgetRows, physicalErrorRows>>
AckWrite(uncertain) == /\ phase = "prepared" /\ uncertain \in BOOLEAN /\ storageAvailable
                       /\ LET ids == IF AckObservedOnly THEN ackSnapshot ELSE Known
                          IN /\ acknowledged' = acknowledged \cup ids
                             /\ createdAcknowledgements' = createdAcknowledgements \cup ids
                       /\ phase' = (IF uncertain THEN "error" ELSE "done")
                       /\ UNCHANGED <<durable, ephemeral, seen, approvedEvents, storageAvailable,
                                      observedEvents, ackSnapshot, expectedCheckpoint, restarted,
                                      sends, unsafeSend, sentAfterAck, probes, budgetRows, physicalErrorRows>>
AckWriteFailure == /\ phase = "prepared" /\ ~storageAvailable /\ phase' = "error"
                   /\ UNCHANGED <<durable, ephemeral, seen, acknowledged, createdAcknowledgements,
                                  approvedEvents, storageAvailable, observedEvents, ackSnapshot,
                                  expectedCheckpoint, restarted, sends, unsafeSend, sentAfterAck,
                                  probes, budgetRows, physicalErrorRows>>
Restart == /\ ~restarted /\ restarted' = TRUE /\ ephemeral' = {}
           /\ phase' = "idle" /\ observedEvents' = {} /\ ackSnapshot' = {} /\ expectedCheckpoint' = <<{}, {}, {}>>
           /\ UNCHANGED <<durable, seen, acknowledged, createdAcknowledgements, approvedEvents,
                          storageAvailable, sends, unsafeSend, sentAfterAck, probes, budgetRows, physicalErrorRows>>
TrySend == /\ sends < 2 /\ Permit /\ sends' = sends + 1
           /\ unsafeSend' = (unsafeSend \/ ~storageAvailable \/ ~(acknowledged \subseteq durable)
                              \/ ~(Known \subseteq acknowledged))
           /\ sentAfterAck' = (sentAfterAck \/ (phase = "done" /\ createdAcknowledgements # {}))
           /\ UNCHANGED <<durable, ephemeral, seen, acknowledged, createdAcknowledgements, approvedEvents,
                          storageAvailable, phase, observedEvents, ackSnapshot, expectedCheckpoint,
                          restarted, probes, budgetRows, physicalErrorRows>>
Next == (\E event \in Events : Observe403(event) \/ PersistEvent(event))
        \/ StorageChange \/ Inspect \/ AckRead \/ (\E uncertain \in BOOLEAN : AckWrite(uncertain))
        \/ AckWriteFailure \/ Restart \/ TrySend
Spec == Init /\ [][Next]_vars
TypeOK == /\ durable \subseteq Events /\ ephemeral \subseteq Events /\ seen \subseteq Events
          /\ acknowledged \subseteq Events /\ createdAcknowledgements \subseteq Events
          /\ approvedEvents \subseteq Events /\ observedEvents \subseteq Events /\ ackSnapshot \subseteq Events
          /\ storageAvailable \in BOOLEAN /\ restarted \in BOOLEAN
          /\ phase \in {"idle", "inspected", "prepared", "rejected", "done", "error"}
          /\ expectedCheckpoint \in (SUBSET Events) \X (SUBSET Events) \X (SUBSET Events)
          /\ sends \in 0..2 /\ unsafeSend \in BOOLEAN /\ sentAfterAck \in BOOLEAN
CheckpointTracksKnownEvents == (phase \in {"inspected", "prepared"} /\ Readable
                               /\ expectedCheckpoint = Checkpoint) => observedEvents = Known
NoNewOrphanAcknowledgement == createdAcknowledgements \subseteq durable
NoAcknowledgementOfUnobservedError == createdAcknowledgements \subseteq approvedEvents
NoStaleAckWithDurableSeed == durable # {} => createdAcknowledgements \subseteq approvedEvents
NoUnsafeAdmission == ~unsafeSend
NoProbeOrLedgerMutation == probes = 0 /\ budgetRows = "unchanged" /\ physicalErrorRows = "unchanged"
SuccessUnreachable == ~sentAfterAck
=============================================================================
