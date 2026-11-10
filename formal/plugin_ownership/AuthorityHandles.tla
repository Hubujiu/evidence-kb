------------------------------ MODULE AuthorityHandles ------------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS GuardInitIdentity, GuardInitClaim, GuardInitPhase, CloseInheritedInit,
          WaitForLeases, GuardResolveGeneration, GuardResolveOwner,
          GuardCleanupCurrent, GuardResolvePhase
VARIABLES claimed, initComplete, phase, generation, cancelled, live, issued,
          cleanup, cleanupFailed, bound, badInit, badBorrow, badResolve,
          earlyCleanup, unclaimedRejected, closedRejected, borrowedAfterCancel,
          resolvedCurrent, staleCleanupRejected, generationRejected, resolvedActive
vars == <<claimed,initComplete,phase,generation,cancelled,live,issued,
          cleanup,cleanupFailed,bound,badInit,badBorrow,badResolve,
          earlyCleanup,unclaimedRejected,closedRejected,borrowedAfterCancel,
          resolvedCurrent,staleCleanupRejected,generationRejected,resolvedActive>>
Flags == <<badInit,badBorrow,badResolve,earlyCleanup,unclaimedRejected,closedRejected,
           borrowedAfterCancel,resolvedCurrent,staleCleanupRejected,generationRejected,resolvedActive>>
Init == /\ claimed = FALSE /\ initComplete = FALSE /\ phase = "starting" /\ generation = 1
        /\ cancelled = FALSE /\ live = {} /\ issued = {} /\ cleanup = 0 /\ cleanupFailed = FALSE /\ bound = TRUE
        /\ badInit = FALSE /\ badBorrow = FALSE /\ badResolve = FALSE /\ earlyCleanup = FALSE
        /\ unclaimedRejected = FALSE /\ closedRejected = FALSE /\ borrowedAfterCancel = FALSE
        /\ resolvedCurrent = FALSE /\ staleCleanupRejected = FALSE /\ generationRejected = FALSE /\ resolvedActive = FALSE
Claim == /\ ~claimed /\ ~initComplete /\ claimed' = TRUE
         /\ UNCHANGED <<initComplete,phase,generation,cancelled,live,issued,cleanup,cleanupFailed,bound,Flags>>
AcquireInit(kind) ==
    /\ 1 \notin issued /\ kind \in {"valid","wrong_identity"}
    /\ LET correct == kind = "valid" /\ claimed /\ ~initComplete /\ phase = "starting"
           accept == (~GuardInitIdentity \/ kind = "valid") /\ (~GuardInitClaim \/ claimed)
                     /\ ~initComplete /\ (~GuardInitPhase \/ phase = "starting")
       IN /\ live' = (IF accept THEN live \cup {1} ELSE live)
          /\ issued' = (IF accept THEN issued \cup {1} ELSE issued)
          /\ badInit' = (badInit \/ (accept /\ ~correct))
          /\ unclaimedRejected' = (unclaimedRejected \/ (~accept /\ ~claimed /\ kind = "valid"))
    /\ UNCHANGED <<claimed,initComplete,phase,generation,cancelled,cleanup,cleanupFailed,bound,
                   badBorrow,badResolve,earlyCleanup,closedRejected,borrowedAfterCancel,resolvedCurrent,
                   staleCleanupRejected,generationRejected,resolvedActive>>
Borrow ==
    /\ 1 \in live /\ 2 \notin issued
    /\ LET accept == ~initComplete \/ ~CloseInheritedInit
       IN /\ live' = (IF accept THEN live \cup {2} ELSE live)
          /\ issued' = (IF accept THEN issued \cup {2} ELSE issued)
          /\ badBorrow' = (badBorrow \/ (accept /\ initComplete))
          /\ closedRejected' = (closedRejected \/ (~accept /\ initComplete))
          /\ borrowedAfterCancel' = (borrowedAfterCancel \/ (accept /\ cancelled /\ ~initComplete))
    /\ UNCHANGED <<claimed,initComplete,phase,generation,cancelled,cleanup,cleanupFailed,bound,
                   badInit,badResolve,earlyCleanup,unclaimedRejected,resolvedCurrent,
                   staleCleanupRejected,generationRejected,resolvedActive>>
ProbeClosed == /\ live # {} /\ initComplete /\ ~closedRejected
               /\ closedRejected' = CloseInheritedInit
               /\ badBorrow' = (badBorrow \/ ~CloseInheritedInit)
               /\ UNCHANGED <<claimed,initComplete,phase,generation,cancelled,live,issued,cleanup,cleanupFailed,bound,
                              badInit,badResolve,earlyCleanup,unclaimedRejected,borrowedAfterCancel,resolvedCurrent,
                              staleCleanupRejected,generationRejected,resolvedActive>>
Release(l) == /\ l \in live /\ live' = live \ {l}
              /\ UNCHANGED <<claimed,initComplete,phase,generation,cancelled,issued,cleanup,cleanupFailed,bound,Flags>>
Cancel == /\ claimed /\ ~initComplete /\ ~cancelled
          /\ cancelled' = TRUE /\ phase' = "draining"
          /\ UNCHANGED <<claimed,initComplete,generation,live,issued,cleanup,cleanupFailed,bound,Flags>>
CompleteInit == /\ claimed /\ ~initComplete /\ initComplete' = TRUE
                /\ phase' = (IF cancelled THEN "draining" ELSE "active")
                /\ UNCHANGED <<claimed,generation,cancelled,live,issued,cleanup,cleanupFailed,bound,Flags>>
Update == /\ phase = "active" /\ phase' = "draining"
          /\ UNCHANGED <<claimed,initComplete,generation,cancelled,live,issued,cleanup,cleanupFailed,bound,Flags>>
BeginCleanup == /\ phase = "draining" /\ initComplete /\ (live = {} \/ ~WaitForLeases)
                /\ phase' = "cleaning" /\ cleanup' = 2 /\ earlyCleanup' = (earlyCleanup \/ live # {})
                /\ UNCHANGED <<claimed,initComplete,generation,cancelled,live,issued,cleanupFailed,bound,
                               badInit,badBorrow,badResolve,unclaimedRejected,closedRejected,borrowedAfterCancel,
                               resolvedCurrent,staleCleanupRejected,generationRejected,resolvedActive>>
FailCleanup == /\ phase = "cleaning" /\ cleanup = 2 /\ ~cleanupFailed
               /\ phase' = "cleanup_failed" /\ cleanup' = 0 /\ cleanupFailed' = TRUE
               /\ UNCHANGED <<claimed,initComplete,generation,cancelled,live,issued,bound,Flags>>
RetryCleanup == /\ phase = "cleanup_failed" /\ phase' = "cleaning" /\ cleanup' = 3
                /\ UNCHANGED <<claimed,initComplete,generation,cancelled,live,issued,cleanupFailed,bound,Flags>>
FinishCleanup == /\ phase = "cleaning" /\ cleanup \in {2,3}
                 /\ phase' = (IF cancelled THEN "failed" ELSE "pending")
                 /\ generation' = (IF cancelled THEN generation ELSE 2) /\ cleanup' = 0 /\ bound' = FALSE
                 /\ UNCHANGED <<claimed,initComplete,cancelled,live,issued,cleanupFailed,Flags>>
Resolve(kind, op) ==
    /\ kind \in {"valid","wrong_owner","wrong_generation"} /\ op \in 0..3
    /\ LET ownerOK == kind # "wrong_owner"
           generationOK == kind # "wrong_generation"
           accessOK == IF op = 0 THEN phase \in {"starting","active"} ELSE op \in {2,3} /\ op = cleanup
           correct == ownerOK /\ generationOK /\ accessOK /\ bound
           access == IF op = 0 THEN (~GuardResolvePhase \/ phase \in {"starting","active"})
                     ELSE op \in {2,3} /\ (~GuardCleanupCurrent \/ op = cleanup)
           accept == (~GuardResolveOwner \/ ownerOK) /\ (~GuardResolveGeneration \/ generationOK) /\ access /\ bound
       IN /\ badResolve' = (badResolve \/ (accept /\ ~correct))
          /\ resolvedCurrent' = (resolvedCurrent \/ (accept /\ kind = "valid" /\ op = cleanup /\ cleanup # 0))
          /\ staleCleanupRejected' = (staleCleanupRejected \/ (~accept /\ kind = "valid" /\ cleanup = 3 /\ op = 2))
          /\ generationRejected' = (generationRejected \/ (~accept /\ kind = "wrong_generation" /\ accessOK /\ bound))
          /\ resolvedActive' = (resolvedActive \/ (accept /\ kind = "valid" /\ op = 0 /\ phase = "active"))
    /\ UNCHANGED <<claimed,initComplete,phase,generation,cancelled,live,issued,cleanup,cleanupFailed,bound,
                   badInit,badBorrow,earlyCleanup,unclaimedRejected,closedRejected,borrowedAfterCancel>>
Next == Claim \/ (\E k \in {"valid","wrong_identity"} : AcquireInit(k)) \/ Borrow \/ ProbeClosed
        \/ (\E l \in {1,2} : Release(l)) \/ Cancel \/ CompleteInit \/ Update \/ BeginCleanup
        \/ FailCleanup \/ RetryCleanup \/ FinishCleanup
        \/ (\E k \in {"valid","wrong_owner","wrong_generation"}, op \in 0..3 : Resolve(k,op))
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"starting","active","draining","cleaning","cleanup_failed","failed","pending"}
          /\ generation \in 1..2 /\ live \subseteq issued /\ issued \subseteq {1,2} /\ cleanup \in {0,2,3}
          /\ {claimed,initComplete,cancelled,cleanupFailed,bound,badInit,badBorrow,badResolve,
                earlyCleanup,unclaimedRejected,closedRejected,borrowedAfterCancel,resolvedCurrent,
                staleCleanupRejected,generationRejected,resolvedActive} \subseteq BOOLEAN
ExactInitAuthority == ~badInit
CompletedInitClosesBorrow == ~badBorrow
ExactResolveAuthority == ~badResolve
NoCleanupWhilePinned == ~earlyCleanup
CancelledAuthoritySuccessUnreachable == ~(cancelled /\ borrowedAfterCancel /\ closedRejected /\ live = {} /\ phase = "failed" /\ resolvedCurrent /\ staleCleanupRejected)
ActiveAuthoritySuccessUnreachable == ~(~cancelled /\ resolvedActive /\ generationRejected /\ resolvedCurrent /\ staleCleanupRejected /\ generation = 2 /\ phase = "pending" /\ live = {})
ClaimedAuthoritySuccessUnreachable == ~(unclaimedRejected /\ 1 \in issued /\ claimed /\ initComplete /\ closedRejected /\ live = {})
=====================================================================================
