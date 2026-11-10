------------------------ MODULE ActivationDispatch ------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS RemoveUnclaimed, RequireAllDependencies, RequireCurrentEpoch, AdvanceGeneration
VARIABLES phase, generation, enabled, dependencies, issued, queued, claimed, completed,
          init, discarded, suppressed, cancelled, published, boundAtStart,
          disabled, reenabled, failed, failureGenerations, reusedIdentity, stalePublication
vars == <<phase, generation, enabled, dependencies, issued, queued, claimed, completed,
          init, discarded, suppressed, cancelled, published, boundAtStart,
          disabled, reenabled, failed, failureGenerations, reusedIdentity, stalePublication>>
Dependencies == {"model", "storage"}
Init == /\ phase = "pending" /\ generation = 1 /\ enabled = TRUE
        /\ dependencies = {} /\ issued = {} /\ queued = 0 /\ init = 0
        /\ claimed = {} /\ completed = {} /\ discarded = {} /\ suppressed = {}
        /\ cancelled = {} /\ published = {} /\ boundAtStart = [g \in 1..2 |-> {}]
        /\ disabled = FALSE /\ reenabled = FALSE /\ failed = FALSE
        /\ failureGenerations = {} /\ reusedIdentity = FALSE /\ stalePublication = FALSE
DependencyReady(d) == /\ d \in Dependencies \ dependencies /\ dependencies' = dependencies \cup {d}
                      /\ UNCHANGED <<phase, generation, enabled, issued, queued, claimed, completed,
                                     init, discarded, suppressed, cancelled, published, boundAtStart,
                                     disabled, reenabled, failed, failureGenerations, reusedIdentity, stalePublication>>
Start == /\ phase = "pending" /\ enabled /\ ~failed /\ init = 0
         /\ (IF RequireAllDependencies THEN dependencies = Dependencies ELSE dependencies # {})
         /\ phase' = "starting" /\ init' = generation /\ queued' = generation
         /\ issued' = issued \cup {generation}
         /\ boundAtStart' = [boundAtStart EXCEPT ![generation] = dependencies]
         /\ reusedIdentity' = (reusedIdentity \/ generation \in issued)
         /\ UNCHANGED <<generation, enabled, dependencies, claimed, completed, discarded,
                        suppressed, cancelled, published, disabled, reenabled, failed,
                        failureGenerations, stalePublication>>
Claim == /\ queued # 0 /\ claimed' = claimed \cup {queued} /\ queued' = 0
         /\ UNCHANGED <<phase, generation, enabled, dependencies, issued, completed, init,
                        discarded, suppressed, cancelled, published, boundAtStart, disabled,
                        reenabled, failed, failureGenerations, reusedIdentity, stalePublication>>
Disable == /\ ~disabled /\ generation = 1 /\ disabled' = TRUE /\ enabled' = FALSE /\ failed' = FALSE
           /\ phase' = (IF phase \in {"pending", "failed"} THEN "pending" ELSE "draining")
           /\ suppressed' = (IF queued # 0 THEN suppressed \cup {queued} ELSE suppressed)
           /\ discarded' = (IF queued # 0 /\ RemoveUnclaimed THEN discarded \cup {queued} ELSE discarded)
           /\ init' = (IF queued # 0 /\ RemoveUnclaimed THEN 0 ELSE init)
           /\ queued' = (IF RemoveUnclaimed THEN 0 ELSE queued)
           /\ cancelled' = (IF init # 0 THEN cancelled \cup {init} ELSE cancelled)
           /\ UNCHANGED <<generation, dependencies, issued, claimed, completed, published,
                          boundAtStart, reenabled, failureGenerations, reusedIdentity, stalePublication>>
Enable == /\ disabled /\ ~reenabled /\ ~enabled /\ generation < 2
          /\ enabled' = TRUE /\ reenabled' = TRUE /\ failed' = FALSE
          /\ generation' = (IF phase \in {"pending", "failed"} /\ AdvanceGeneration
                             THEN generation + 1 ELSE generation)
          /\ phase' = (IF phase = "failed" THEN "pending" ELSE phase)
          /\ UNCHANGED <<dependencies, issued, queued, claimed, completed, init, discarded,
                         suppressed, cancelled, published, boundAtStart, disabled,
                         failureGenerations, reusedIdentity, stalePublication>>
CancelInit == /\ phase = "starting" /\ init \in claimed /\ init \notin cancelled
              /\ phase' = "draining" /\ failed' = TRUE /\ cancelled' = cancelled \cup {init}
              /\ failureGenerations' = failureGenerations \cup {generation}
              /\ UNCHANGED <<generation, enabled, dependencies, issued, queued, claimed, completed,
                             init, discarded, suppressed, published, boundAtStart, disabled,
                             reenabled, reusedIdentity, stalePublication>>
Complete(ok) ==
    /\ init # 0 /\ init \in claimed /\ init \notin completed /\ ok \in BOOLEAN
    /\ LET current == phase = "starting"
           publish == ok /\ enabled /\ dependencies = Dependencies
                      /\ (~RequireCurrentEpoch \/ current)
       IN /\ published' = (IF publish THEN published \cup {generation} ELSE published)
          /\ stalePublication' = (stalePublication \/ (publish /\ ~current))
          /\ phase' = (IF publish THEN "active" ELSE "draining")
          /\ failed' = (failed \/ (~ok /\ current))
          /\ failureGenerations' = (IF ~ok /\ current THEN failureGenerations \cup {generation} ELSE failureGenerations)
    /\ completed' = completed \cup {init} /\ init' = 0
    /\ UNCHANGED <<generation, enabled, dependencies, issued, queued, claimed, discarded,
                   suppressed, cancelled, boundAtStart, disabled, reenabled, reusedIdentity>>
Clean == /\ phase = "draining" /\ init = 0 /\ generation < 2
         /\ phase' = (IF failed THEN "failed" ELSE "pending")
         /\ generation' = (IF ~failed /\ AdvanceGeneration THEN generation + 1 ELSE generation)
         /\ UNCHANGED <<enabled, dependencies, issued, queued, claimed, completed, init,
                        discarded, suppressed, cancelled, published, boundAtStart, disabled,
                        reenabled, failed, failureGenerations, reusedIdentity, stalePublication>>
Next == (\E d \in Dependencies : DependencyReady(d)) \/ Start \/ Claim \/ Disable \/ Enable
        \/ CancelInit \/ (\E ok \in BOOLEAN : Complete(ok)) \/ Clean
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"pending", "starting", "active", "draining", "failed"}
          /\ generation \in 1..2 /\ enabled \in BOOLEAN /\ dependencies \subseteq Dependencies
          /\ issued \subseteq 1..2 /\ queued \in 0..2 /\ init \in 0..2
          /\ claimed \subseteq issued /\ completed \subseteq claimed /\ discarded \subseteq issued
          /\ suppressed \subseteq issued /\ cancelled \subseteq issued /\ published \subseteq issued
          /\ boundAtStart \in [1..2 -> SUBSET Dependencies] /\ disabled \in BOOLEAN /\ reenabled \in BOOLEAN
          /\ failed \in BOOLEAN /\ failureGenerations \subseteq issued
          /\ reusedIdentity \in BOOLEAN /\ stalePublication \in BOOLEAN
AllDependenciesBeforeDispatch == \A g \in issued : boundAtStart[g] = Dependencies
NoSuppressedInitDispatch == claimed \cap suppressed = {}
NoSupersededPublication == ~stalePublication
NoGenerationReuse == ~reusedIdentity
FailedEpochNeverPublishes == failureGenerations \cap published = {}
UnclaimedRecoveryUnreachable == ~(1 \in discarded /\ 2 \in published /\ reenabled)
ClaimedRecoveryUnreachable == ~(1 \in claimed \cap completed \cap cancelled /\ 2 \in published /\ reenabled)
CancelledFailureUnreachable == ~(phase = "failed" /\ 1 \in claimed \cap completed \cap cancelled)
=============================================================================
