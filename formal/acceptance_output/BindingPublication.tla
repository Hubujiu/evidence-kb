---------------------- MODULE BindingPublication ----------------------
EXTENDS Naturals, FiniteSets, Sequences, TLC
CONSTANTS PreserveScenario, CheckFinalAuthority
VARIABLES pc, status, epoch, scenario, first, last, split, file, artifacts,
          original, descriptor, observed, crashed, restarted, rejected, sends, receipts
vars == <<pc, status, epoch, scenario, first, last, split, file, artifacts,
          original, descriptor, observed, crashed, restarted, rejected, sends, receipts>>
Splits == 1..2
Files == 1..9
Init == /\ pc = "receipt" /\ status = "DATA_READY"
        /\ epoch = 1 /\ scenario = 1
        /\ first = [s \in Splits |-> 0] /\ last = [s \in Splits |-> 0]
        /\ split = 1 /\ file = 1
        /\ artifacts = [f \in Files |-> 0] /\ original = artifacts
        /\ descriptor = <<>> /\ observed = <<>>
        /\ crashed = FALSE /\ restarted = FALSE /\ rejected = FALSE /\ sends = 0 /\ receipts \in {"COMPLETE", "UNKNOWN", "MISMATCH"}
ReceiptCheck == /\ pc = "receipt"
                /\ pc' = IF receipts = "COMPLETE" THEN "before" ELSE "rejected"
                /\ rejected' = (receipts # "COMPLETE")
                /\ UNCHANGED <<status, epoch, scenario, first, last, split, file, artifacts, original,
                               descriptor, observed, crashed, restarted, sends, receipts>>
Before == /\ pc = "before"
          /\ first' = [first EXCEPT ![split] = epoch]
          /\ pc' = IF split = 2 THEN "binding" ELSE "before"
          /\ split' = IF split = 2 THEN 1 ELSE 2
          /\ UNCHANGED <<status, epoch, scenario, last, file, artifacts, original,
                         descriptor, observed, crashed, restarted, rejected, sends, receipts>>
Binding == /\ pc = "binding"
           /\ pc' = IF first[1] = first[2] THEN "pre" ELSE "rejected"
           /\ status' = IF first[1] = first[2] THEN "BINDING" ELSE status
           /\ rejected' = (first[1] # first[2])
           /\ UNCHANGED <<epoch, scenario, first, last, split, file, artifacts, original,
                          descriptor, observed, crashed, restarted, sends, receipts>>
Boundary == /\ pc \in {"pre", "bind", "post"}
            /\ pc' = IF first[split] # epoch THEN "rejected"
                      ELSE IF pc = "pre" THEN "bind" ELSE IF pc = "bind" THEN "post" ELSE "files"
            /\ rejected' = (first[split] # epoch)
            /\ UNCHANGED <<status, epoch, scenario, first, last, split, file, artifacts, original,
                           descriptor, observed, crashed, restarted, sends, receipts>>
WriteFile == /\ pc = "files"
             /\ LET slot == (split - 1) * 4 + file
                    valid == artifacts[slot] \in {0, first[split]}
                IN /\ pc' = IF ~valid THEN "rejected" ELSE IF file = 4
                            THEN IF split = 2 THEN "final" ELSE "pre" ELSE "files"
                   /\ artifacts' = IF valid THEN [artifacts EXCEPT ![slot] = first[split]] ELSE artifacts
                   /\ original' = IF valid /\ original[slot] = 0
                                   THEN [original EXCEPT ![slot] = first[split]] ELSE original
                   /\ rejected' = ~valid
             /\ split' = IF file = 4 THEN IF split = 2 THEN 1 ELSE 2 ELSE split
             /\ file' = IF file = 4 THEN 1 ELSE file + 1
             /\ UNCHANGED <<status, epoch, scenario, first, last, descriptor, observed, crashed, restarted, sends, receipts>>
FinalRead == /\ pc = "final"
             /\ last' = [last EXCEPT ![split] = epoch]
             /\ rejected' = (CheckFinalAuthority /\ first[split] # epoch)
             /\ pc' = IF CheckFinalAuthority /\ first[split] # epoch THEN "rejected"
                       ELSE IF split = 2 THEN "scenario" ELSE "final"
             /\ split' = IF split = 2 THEN 1 ELSE 2
             /\ UNCHANGED <<status, epoch, scenario, first, file, artifacts, original,
                            descriptor, observed, crashed, restarted, sends, receipts>>
Scenario == /\ pc = "scenario"
            /\ LET valid == artifacts[9] \in {0, scenario} \/ ~PreserveScenario
               IN /\ pc' = IF valid THEN "descriptor" ELSE "rejected"
                  /\ rejected' = ~valid
                  /\ artifacts' = IF valid THEN [artifacts EXCEPT ![9] = scenario] ELSE artifacts
                  /\ original' = IF valid /\ original[9] = 0 THEN [original EXCEPT ![9] = scenario] ELSE original
            /\ UNCHANGED <<status, epoch, scenario, first, last, split, file,
                           descriptor, observed, crashed, restarted, sends, receipts>>
Publish == /\ pc = "descriptor"
           /\ LET value == <<first[1], first[2], scenario>>
                  valid == descriptor \in {<<>>, value}
              IN /\ descriptor' = IF valid THEN value ELSE descriptor
                 /\ observed' = IF valid THEN <<last[1], last[2]>> ELSE observed
                 /\ pc' = IF valid THEN "complete" ELSE "rejected"
                 /\ rejected' = ~valid
           /\ UNCHANGED <<status, epoch, scenario, first, last, split, file, artifacts, original,
                          crashed, restarted, sends, receipts>>
Complete == /\ pc = "complete" /\ pc' = "done" /\ status' = "COMPLETE"
            /\ UNCHANGED <<epoch, scenario, first, last, split, file, artifacts, original,
                           descriptor, observed, crashed, restarted, rejected, sends, receipts>>
ChangeAuthority == /\ epoch = 1 /\ epoch' = 2
                   /\ UNCHANGED <<pc, status, scenario, first, last, split, file, artifacts, original,
                                  descriptor, observed, crashed, restarted, rejected, sends, receipts>>
ChangeScenario == /\ scenario = 1 /\ scenario' = 2
                  /\ UNCHANGED <<pc, status, epoch, first, last, split, file, artifacts, original,
                                 descriptor, observed, crashed, restarted, rejected, sends, receipts>>
Crash == /\ pc \in {"receipt", "before", "binding", "pre", "bind", "post", "files", "final", "scenario", "descriptor", "complete"}
         /\ ~crashed /\ pc' = "crashed" /\ crashed' = TRUE
         /\ UNCHANGED <<status, epoch, scenario, first, last, split, file, artifacts, original,
                        descriptor, observed, restarted, rejected, sends, receipts>>
Restart == /\ pc = "crashed" /\ ~restarted
           /\ pc' = "receipt" /\ status' = "DATA_READY" /\ restarted' = TRUE
           /\ first' = [s \in Splits |-> 0] /\ last' = first'
           /\ split' = 1 /\ file' = 1
           /\ UNCHANGED <<epoch, scenario, artifacts, original, descriptor, observed, crashed, rejected, sends, receipts>>
Next == ReceiptCheck \/ Before \/ Binding \/ Boundary \/ WriteFile \/ FinalRead \/ Scenario \/ Publish \/ Complete \/
        ChangeAuthority \/ ChangeScenario \/ Crash \/ Restart
Spec == Init /\ [][Next]_vars
TypeOK == /\ pc \in {"receipt", "before", "binding", "pre", "bind", "post", "files", "final", "scenario", "descriptor", "complete", "done", "crashed", "rejected"}
          /\ status \in {"DATA_READY", "BINDING", "COMPLETE"}
          /\ epoch \in 1..2 /\ scenario \in 1..2 /\ split \in Splits /\ file \in 1..4
          /\ first \in [Splits -> 0..2] /\ last \in [Splits -> 0..2]
          /\ artifacts \in [Files -> 0..2] /\ original \in [Files -> 0..2]
          /\ crashed \in BOOLEAN /\ restarted \in BOOLEAN /\ rejected \in BOOLEAN /\ sends = 0
          /\ receipts \in {"COMPLETE", "UNKNOWN", "MISMATCH"}
ImmutableArtifacts == \A f \in Files : original[f] # 0 => artifacts[f] = original[f]
DescriptorHasAllFiles == descriptor # <<>> => \A f \in Files : artifacts[f] # 0
ObservedAuthorityAgrees == descriptor # <<>> => <<descriptor[1], descriptor[2]>> = observed
CompleteHasDescriptor == status = "COMPLETE" => descriptor # <<>> /\ \A f \in Files : artifacts[f] # 0
NoNewRemoteEffects == sends = 0
ReceiptReuseSafe == descriptor # <<>> => receipts = "COMPLETE"
NoCompleteWitness == status # "COMPLETE"
NoPartialCrashWitness == ~(pc = "crashed" /\ artifacts[1] # 0 /\ artifacts[8] = 0)
=============================================================================
