------------------------------ MODULE FailedInitRetry ------------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS RequireExplicitRetry, ResetFailure, AdvanceGeneration, RespectClosedScope
VARIABLES phase, generation, failed, error, retried, explicitRetry, closed,
          cleanupFault, cleanupRetry, oldResult, initOperation, allocated,
          automaticRecovery, closedDispatch, duplicateObserved
vars == <<phase,generation,failed,error,retried,explicitRetry,closed,
          cleanupFault,cleanupRetry,oldResult,initOperation,allocated,
          automaticRecovery,closedDispatch,duplicateObserved>>
Init == /\ phase = "starting" /\ generation = 1 /\ failed = FALSE /\ error = FALSE
        /\ retried = FALSE /\ explicitRetry = FALSE /\ closed = FALSE
        /\ cleanupFault = FALSE /\ cleanupRetry = FALSE /\ oldResult = "pending"
        /\ initOperation = 1 /\ allocated = {1}
        /\ automaticRecovery = FALSE /\ closedDispatch = FALSE /\ duplicateObserved = FALSE
FailInit == /\ phase = "starting" /\ initOperation = 1
            /\ phase' = "draining" /\ failed' = TRUE /\ error' = TRUE /\ oldResult' = "error"
            /\ UNCHANGED <<generation,retried,explicitRetry,closed,cleanupFault,cleanupRetry,
                           initOperation,allocated,automaticRecovery,closedDispatch,duplicateObserved>>
BeginCleanup == /\ phase = "draining" /\ phase' = "cleaning"
                /\ UNCHANGED <<generation,failed,error,retried,explicitRetry,closed,cleanupFault,
                               cleanupRetry,oldResult,initOperation,allocated,automaticRecovery,
                               closedDispatch,duplicateObserved>>
CleanupError == /\ phase = "cleaning" /\ ~cleanupFault
                /\ phase' = "cleanup_failed" /\ cleanupFault' = TRUE
                /\ UNCHANGED <<generation,failed,error,retried,explicitRetry,closed,cleanupRetry,
                               oldResult,initOperation,allocated,automaticRecovery,closedDispatch,duplicateObserved>>
RetryCleanup == /\ phase = "cleanup_failed" /\ phase' = "cleaning" /\ cleanupRetry' = TRUE
                /\ UNCHANGED <<generation,failed,error,retried,explicitRetry,closed,cleanupFault,
                               oldResult,initOperation,allocated,automaticRecovery,closedDispatch,duplicateObserved>>
FinishCleanup == /\ phase = "cleaning" /\ phase' = "failed"
                 /\ UNCHANGED <<generation,failed,error,retried,explicitRetry,closed,cleanupFault,
                                cleanupRetry,oldResult,initOperation,allocated,automaticRecovery,
                                closedDispatch,duplicateObserved>>
CloseScope == /\ phase = "failed" /\ ~closed /\ closed' = TRUE
              /\ UNCHANGED <<phase,generation,failed,error,retried,explicitRetry,cleanupFault,
                             cleanupRetry,oldResult,initOperation,allocated,automaticRecovery,
                             closedDispatch,duplicateObserved>>
RetryInit(operator) ==
    /\ phase = "failed" /\ ~retried /\ operator \in BOOLEAN /\ (operator \/ ~RequireExplicitRetry)
    /\ phase' = "pending" /\ retried' = TRUE /\ explicitRetry' = operator
    /\ failed' = (IF ResetFailure THEN FALSE ELSE failed)
    /\ error' = (IF ResetFailure THEN FALSE ELSE error)
    /\ generation' = (IF AdvanceGeneration THEN 2 ELSE generation)
    /\ automaticRecovery' = (automaticRecovery \/ ~operator)
    /\ UNCHANGED <<closed,cleanupFault,cleanupRetry,oldResult,initOperation,allocated,
                   closedDispatch,duplicateObserved>>
Dispatch == /\ phase = "pending" /\ ~failed /\ (~closed \/ ~RespectClosedScope)
            /\ phase' = "starting" /\ initOperation' = 2 /\ allocated' = allocated \cup {2}
            /\ closedDispatch' = (closedDispatch \/ closed)
            /\ UNCHANGED <<generation,failed,error,retried,explicitRetry,closed,cleanupFault,
                           cleanupRetry,oldResult,automaticRecovery,duplicateObserved>>
CompleteRetry == /\ phase = "starting" /\ initOperation = 2 /\ phase' = "active"
                 /\ UNCHANGED <<generation,failed,error,retried,explicitRetry,closed,cleanupFault,
                                cleanupRetry,oldResult,initOperation,allocated,automaticRecovery,
                                closedDispatch,duplicateObserved>>
DuplicateOld == /\ oldResult = "error" /\ retried /\ ~duplicateObserved
                /\ duplicateObserved' = TRUE
                /\ UNCHANGED <<phase,generation,failed,error,retried,explicitRetry,closed,cleanupFault,
                               cleanupRetry,oldResult,initOperation,allocated,automaticRecovery,closedDispatch>>
Next == FailInit \/ BeginCleanup \/ CleanupError \/ RetryCleanup \/ FinishCleanup \/ CloseScope
        \/ (\E operator \in BOOLEAN : RetryInit(operator)) \/ Dispatch \/ CompleteRetry \/ DuplicateOld
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"starting","draining","cleaning","cleanup_failed","failed","pending","active"}
          /\ generation \in 1..2 /\ initOperation \in {1,2} /\ allocated \subseteq {1,2}
          /\ oldResult \in {"pending","error"}
          /\ {failed,error,retried,explicitRetry,closed,cleanupFault,cleanupRetry,
                automaticRecovery,closedDispatch,duplicateObserved} \subseteq BOOLEAN
NoAutomaticRecovery == ~automaticRecovery
RetryClearsFailure == retried => ~failed /\ ~error
RetryAdvancesGeneration == retried => generation = 2
NoClosedDispatch == ~closedDispatch
HistoricalFailurePreserved == retried => oldResult = "error"
FreshInitIdentity == initOperation = 2 => allocated = {1,2} /\ retried
RecoverySuccessUnreachable == ~(phase = "active" /\ explicitRetry /\ cleanupFault /\ cleanupRetry /\ duplicateObserved /\ oldResult = "error" /\ allocated = {1,2})
ClosedRetrySuccessUnreachable == ~(phase = "pending" /\ closed /\ explicitRetry /\ generation = 2 /\ ~failed /\ ~error /\ duplicateObserved /\ allocated = {1})
FailureHeldUnreachable == ~(phase = "failed" /\ failed /\ error /\ ~retried /\ cleanupFault /\ cleanupRetry /\ oldResult = "error")
===================================================================================
