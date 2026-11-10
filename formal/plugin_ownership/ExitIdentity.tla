------------------------------ MODULE ExitIdentity ------------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS MatchScope, MatchInstance, MatchGeneration, MatchPhase
VARIABLES current, generation, phase, desired, replaced, readded,
          wrongScope, wrongInstance, wrongGeneration, wrongPhase,
          oldGenerationIgnored, oldIdentityIgnored, namespaceIgnored,
          duplicateIgnored, acceptedStarting, acceptedActive
vars == <<current, generation, phase, desired, replaced, readded,
          wrongScope, wrongInstance, wrongGeneration, wrongPhase,
          oldGenerationIgnored, oldIdentityIgnored, namespaceIgnored,
          duplicateIgnored, acceptedStarting, acceptedActive>>
Init == /\ current = 10 /\ generation = 1 /\ phase = "starting" /\ desired = "enabled"
        /\ replaced = FALSE /\ readded = FALSE
        /\ wrongScope = FALSE /\ wrongInstance = FALSE /\ wrongGeneration = FALSE /\ wrongPhase = FALSE
        /\ oldGenerationIgnored = FALSE /\ oldIdentityIgnored = FALSE /\ namespaceIgnored = FALSE
        /\ duplicateIgnored = FALSE /\ acceptedStarting = FALSE /\ acceptedActive = FALSE
Flags == <<wrongScope, wrongInstance, wrongGeneration, wrongPhase,
           oldGenerationIgnored, oldIdentityIgnored, namespaceIgnored,
           duplicateIgnored, acceptedStarting, acceptedActive>>
Publish == /\ phase = "starting" /\ desired = "enabled" /\ phase' = "active"
           /\ UNCHANGED <<current, generation, desired, replaced, readded, Flags>>
Update == /\ current = 10 /\ generation = 1 /\ phase = "active" /\ desired = "enabled" /\ ~replaced
          /\ phase' = "draining" /\ replaced' = TRUE
          /\ UNCHANGED <<current, generation, desired, readded, Flags>>
Remove == /\ phase \in {"starting","active","pending"} /\ desired # "removed"
          /\ desired' = "removed" /\ phase' = "draining"
          /\ UNCHANGED <<current, generation, replaced, readded, Flags>>
Finish == /\ phase = "draining"
          /\ phase' = (CASE desired = "removed" -> "disposed"
                       [] desired = "disabled" -> "pending" [] OTHER -> "starting")
          /\ generation' = (IF desired = "enabled" THEN 2 ELSE generation)
          /\ UNCHANGED <<current, desired, replaced, readded, Flags>>
Readd == /\ phase = "disposed" /\ current = 10 /\ ~readded
         /\ current' = 20 /\ generation' = 1 /\ phase' = "starting" /\ desired' = "enabled" /\ readded' = TRUE
         /\ UNCHANGED <<replaced, Flags>>
Notice(h, plugin, id, gen) ==
    /\ h \in {1,2} /\ plugin \in {"p","other"} /\ id \in {10,20} /\ gen \in 1..2
    /\ LET scopeOK == h = 1 /\ plugin = "p"
           idOK == id = current
           genOK == gen = generation
           phaseOK == phase \in {"starting","active"}
           accept == (~MatchScope \/ scopeOK) /\ (~MatchInstance \/ idOK)
                     /\ (~MatchGeneration \/ genOK) /\ (~MatchPhase \/ phaseOK) /\ desired = "enabled"
       IN /\ desired' = (IF accept THEN "disabled" ELSE desired)
          /\ phase' = (IF accept THEN "draining" ELSE phase)
          /\ wrongScope' = (wrongScope \/ (accept /\ ~scopeOK))
          /\ wrongInstance' = (wrongInstance \/ (accept /\ ~idOK))
          /\ wrongGeneration' = (wrongGeneration \/ (accept /\ ~genOK))
          /\ wrongPhase' = (wrongPhase \/ (accept /\ ~phaseOK))
          /\ oldGenerationIgnored' = (oldGenerationIgnored \/ (~accept /\ scopeOK /\ current = 10 /\ generation = 2 /\ id = 10 /\ gen = 1 /\ phaseOK))
          /\ oldIdentityIgnored' = (oldIdentityIgnored \/ (~accept /\ scopeOK /\ current = 20 /\ generation = 1 /\ id = 10 /\ gen = 1 /\ phaseOK))
          /\ namespaceIgnored' = (namespaceIgnored \/ (~accept /\ ~scopeOK /\ idOK /\ genOK /\ phaseOK))
          /\ duplicateIgnored' = (duplicateIgnored \/ (~accept /\ scopeOK /\ idOK /\ genOK /\ desired = "disabled"))
          /\ acceptedStarting' = (acceptedStarting \/ (accept /\ current = 20 /\ phase = "starting"))
          /\ acceptedActive' = (acceptedActive \/ (accept /\ current = 20 /\ phase = "active"))
    /\ UNCHANGED <<current, generation, replaced, readded>>
Next == Publish \/ Update \/ Remove \/ Finish \/ Readd
        \/ (\E h \in {1,2}, p \in {"p","other"}, id \in {10,20}, g \in 1..2 : Notice(h,p,id,g))
Spec == Init /\ [][Next]_vars
TypeOK == /\ current \in {10,20} /\ generation \in 1..2
          /\ phase \in {"starting","active","draining","pending","disposed"}
          /\ desired \in {"enabled","disabled","removed"}
          /\ {replaced,readded,wrongScope,wrongInstance,wrongGeneration,wrongPhase,
                oldGenerationIgnored,oldIdentityIgnored,namespaceIgnored,
                duplicateIgnored,acceptedStarting,acceptedActive} \subseteq BOOLEAN
NoWrongScope == ~wrongScope
NoWrongInstance == ~wrongInstance
NoWrongGeneration == ~wrongGeneration
NoWrongPhase == ~wrongPhase
NewIdentityNeverReverts == readded => current = 20
StartingExitSuccessUnreachable == ~(current = 20 /\ oldIdentityIgnored /\ namespaceIgnored /\ duplicateIgnored /\ acceptedStarting /\ desired = "disabled")
ActiveExitSuccessUnreachable == ~(current = 20 /\ oldIdentityIgnored /\ namespaceIgnored /\ duplicateIgnored /\ acceptedActive /\ desired = "disabled")
GenerationExitSuccessUnreachable == ~(oldGenerationIgnored /\ duplicateIgnored /\ current = 10 /\ generation = 2 /\ desired = "disabled")
=================================================================================
