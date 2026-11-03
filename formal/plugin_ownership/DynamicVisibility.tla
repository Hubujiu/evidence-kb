------------------------ MODULE DynamicVisibility ------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS Isolated, RespectIsolation, StopInactiveShadow, ExactAdmittedBinding, MergeAncestors
VARIABLES localPhase, pinIssued, pinLive, pinRegistration, nextCall, outstanding,
          delivered, callRegistration, bindingRegistration, wrongIdentity,
          wrongIntercept, wrongFresh, isolationBypass, retainedPending,
          retainedActive, freshLocal
vars == <<localPhase, pinIssued, pinLive, pinRegistration, nextCall, outstanding,
          delivered, callRegistration, bindingRegistration, wrongIdentity,
          wrongIntercept, wrongFresh, isolationBypass, retainedPending,
          retainedActive, freshLocal>>
RootRegistration == 10
LocalRegistration == 20
Registrations == {RootRegistration, LocalRegistration}
RootVisible == ~Isolated \/ ~RespectIsolation
Fresh == IF localPhase = "active" THEN LocalRegistration
         ELSE IF localPhase \in {"starting", "draining"} /\ StopInactiveShadow THEN 0
         ELSE IF RootVisible THEN RootRegistration ELSE 0
CorrectFresh == IF localPhase = "active" THEN LocalRegistration
                ELSE IF localPhase \in {"starting", "draining"} THEN 0
                ELSE IF ~Isolated THEN RootRegistration ELSE 0
Exists(reg) == reg = RootRegistration \/ localPhase \in {"active", "draining"}
MetadataVisible == IF localPhase \in {"active", "draining"} THEN LocalRegistration
                   ELSE IF localPhase = "starting" THEN 0
                   ELSE IF RootVisible THEN RootRegistration ELSE 0
ExpectedIntercept(reg) == <<IF reg = RootRegistration THEN "root" ELSE "none", "child">>
ActualIntercept(reg) == <<IF reg = RootRegistration /\ MergeAncestors THEN "root" ELSE "none", "child">>
Init == /\ localPhase = "absent" /\ pinIssued = FALSE /\ pinLive = FALSE /\ pinRegistration = 0
        /\ nextCall = 1 /\ outstanding = {} /\ delivered = {}
        /\ callRegistration = [c \in 1..2 |-> 0] /\ bindingRegistration = [c \in 1..2 |-> 0]
        /\ wrongIdentity = FALSE /\ wrongIntercept = FALSE /\ wrongFresh = FALSE
        /\ isolationBypass = FALSE /\ retainedPending = FALSE /\ retainedActive = FALSE /\ freshLocal = FALSE
Pin == /\ ~pinIssued /\ pinIssued' = TRUE /\ pinLive' = TRUE /\ pinRegistration' = Fresh
       /\ isolationBypass' = (isolationBypass \/ (Isolated /\ Fresh = RootRegistration))
       /\ UNCHANGED <<localPhase, nextCall, outstanding, delivered, callRegistration, bindingRegistration,
                      wrongIdentity, wrongIntercept, wrongFresh, retainedPending, retainedActive, freshLocal>>
ReleasePin == /\ pinLive /\ pinLive' = FALSE
              /\ UNCHANGED <<localPhase, pinIssued, pinRegistration, nextCall, outstanding, delivered,
                             callRegistration, bindingRegistration, wrongIdentity, wrongIntercept,
                             wrongFresh, isolationBypass, retainedPending, retainedActive, freshLocal>>
Install == /\ localPhase = "absent" /\ localPhase' = "starting"
           /\ UNCHANGED <<pinIssued, pinLive, pinRegistration, nextCall, outstanding, delivered,
                          callRegistration, bindingRegistration, wrongIdentity, wrongIntercept,
                          wrongFresh, isolationBypass, retainedPending, retainedActive, freshLocal>>
Publish == /\ localPhase = "starting" /\ localPhase' = "active"
           /\ UNCHANGED <<pinIssued, pinLive, pinRegistration, nextCall, outstanding, delivered,
                          callRegistration, bindingRegistration, wrongIdentity, wrongIntercept,
                          wrongFresh, isolationBypass, retainedPending, retainedActive, freshLocal>>
Remove == /\ localPhase = "active" /\ localPhase' = "draining"
          /\ UNCHANGED <<pinIssued, pinLive, pinRegistration, nextCall, outstanding, delivered,
                         callRegistration, bindingRegistration, wrongIdentity, wrongIntercept,
                         wrongFresh, isolationBypass, retainedPending, retainedActive, freshLocal>>
Dispose == /\ localPhase = "draining" /\ ~(pinLive /\ pinRegistration = LocalRegistration)
           /\ ~\E c \in outstanding : callRegistration[c] = LocalRegistration
           /\ localPhase' = "disposed"
           /\ UNCHANGED <<pinIssued, pinLive, pinRegistration, nextCall, outstanding, delivered,
                          callRegistration, bindingRegistration, wrongIdentity, wrongIntercept,
                          wrongFresh, isolationBypass, retainedPending, retainedActive, freshLocal>>
Invoke(reg, borrowed) ==
    /\ reg \in Registrations /\ borrowed \in BOOLEAN /\ nextCall <= 2 /\ Exists(reg)
    /\ (IF borrowed THEN pinLive /\ pinRegistration = reg ELSE reg = Fresh)
    /\ ~\E c \in outstanding : callRegistration[c] = reg
    /\ LET binding == IF ExactAdmittedBinding THEN reg ELSE MetadataVisible
       IN /\ bindingRegistration' = [bindingRegistration EXCEPT ![nextCall] = binding]
          /\ delivered' = (IF binding # 0 THEN delivered \cup {nextCall} ELSE delivered)
          /\ wrongIdentity' = (wrongIdentity \/ (binding # 0 /\ binding # reg))
          /\ wrongIntercept' = (wrongIntercept \/ (binding # 0 /\ ActualIntercept(binding) # ExpectedIntercept(reg)))
          /\ retainedPending' = (retainedPending \/ (borrowed /\ reg = RootRegistration
                                                       /\ localPhase = "starting" /\ binding = reg))
          /\ retainedActive' = (retainedActive \/ (borrowed /\ reg = RootRegistration
                                                     /\ localPhase = "active" /\ binding = reg))
          /\ freshLocal' = (freshLocal \/ (~borrowed /\ reg = LocalRegistration /\ binding = reg))
    /\ callRegistration' = [callRegistration EXCEPT ![nextCall] = reg]
    /\ outstanding' = outstanding \cup {nextCall} /\ nextCall' = nextCall + 1
    /\ wrongFresh' = (wrongFresh \/ (~borrowed /\ reg # CorrectFresh))
    /\ isolationBypass' = (isolationBypass \/ (Isolated /\ reg = RootRegistration))
    /\ UNCHANGED <<localPhase, pinIssued, pinLive, pinRegistration>>
Complete(c) == /\ c \in outstanding \cap delivered /\ outstanding' = outstanding \ {c}
               /\ UNCHANGED <<localPhase, pinIssued, pinLive, pinRegistration, nextCall, delivered,
                              callRegistration, bindingRegistration, wrongIdentity, wrongIntercept,
                              wrongFresh, isolationBypass, retainedPending, retainedActive, freshLocal>>
Next == Pin \/ ReleasePin \/ Install \/ Publish \/ Remove \/ Dispose
        \/ (\E reg \in Registrations, borrowed \in BOOLEAN : Invoke(reg, borrowed))
        \/ (\E c \in 1..2 : Complete(c))
Spec == Init /\ [][Next]_vars
TypeOK == /\ localPhase \in {"absent", "starting", "active", "draining", "disposed"}
          /\ {pinIssued, pinLive, wrongIdentity, wrongIntercept, wrongFresh, isolationBypass,
                retainedPending, retainedActive, freshLocal} \subseteq BOOLEAN
          /\ pinRegistration \in Registrations \cup {0} /\ nextCall \in 1..3
          /\ outstanding \subseteq 1..2 /\ delivered \subseteq 1..2
          /\ callRegistration \in [1..2 -> Registrations \cup {0}]
          /\ bindingRegistration \in [1..2 -> Registrations \cup {0}]
AdmittedIdentityPreserved == ~wrongIdentity
DefinitionInterceptPreserved == ~wrongIntercept
NoStrandedInvocation == outstanding \subseteq delivered
NoFreshShadowBypass == ~wrongFresh
NoIsolationBypass == ~isolationBypass
ImmutableAllocation == \A c \in nextCall..2 : callRegistration[c] = 0 /\ bindingRegistration[c] = 0
RetainedSuccessUnreachable == ~(retainedPending /\ retainedActive /\ localPhase = "disposed" /\ ~pinLive /\ outstanding = {})
FreshLocalSuccessUnreachable == ~freshLocal
=============================================================================
