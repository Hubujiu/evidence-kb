--------------------------- MODULE HttpLifespan ---------------------------
EXTENDS Naturals
CONSTANTS MarkEntered, MarkAttemptedExit, MarkOnlyFallbackSuccess, SkipFinished
VARIABLES phase, entered, finished, exits, fallbacks, entryFailed, exitFailed, fallbackFailed
vars == <<phase,entered,finished,exits,fallbacks,entryFailed,exitFailed,fallbackFailed>>
Init == /\ phase = "ready" /\ entered = FALSE /\ finished = FALSE /\ exits = 0
        /\ fallbacks = 0 /\ entryFailed = FALSE /\ exitFailed = FALSE /\ fallbackFailed = FALSE
Enter(ok) == /\ phase = "ready" /\ ok \in BOOLEAN
             /\ phase' = (IF ok THEN "serving" ELSE "run-ended")
             /\ entered' = (ok /\ MarkEntered) /\ entryFailed' = ~ok
             /\ UNCHANGED <<finished,exits,fallbacks,exitFailed,fallbackFailed>>
Exit(ok) == /\ phase = "serving" /\ ok \in BOOLEAN /\ phase' = "run-ended"
            /\ exits' = exits + 1 /\ exitFailed' = ~ok
            /\ finished' = (entered /\ (MarkAttemptedExit \/ ok))
            /\ UNCHANGED <<entered,fallbacks,entryFailed,fallbackFailed>>
Close(ok) == /\ phase \in {"ready","run-ended","closed"} /\ ok \in BOOLEAN /\ fallbacks < 2
             /\ phase' = "closed"
             /\ fallbacks' = (IF finished /\ SkipFinished THEN fallbacks ELSE fallbacks + 1)
             /\ finished' = (IF finished /\ SkipFinished THEN finished ELSE ok \/ ~MarkOnlyFallbackSuccess)
             /\ fallbackFailed' = (IF finished /\ SkipFinished THEN fallbackFailed ELSE ~ok)
             /\ UNCHANGED <<entered,exits,entryFailed,exitFailed>>
Next == (\E ok \in BOOLEAN : Enter(ok) \/ Exit(ok) \/ Close(ok))
Spec == Init /\ [][Next]_vars
TypeOK == /\ phase \in {"ready","serving","run-ended","closed"}
          /\ exits \in 0..1 /\ fallbacks \in 0..2
          /\ {entered,finished,entryFailed,exitFailed,fallbackFailed} \subseteq BOOLEAN
ExitIsOwner == exits = 1 => entered /\ finished
NoSecondShutdownAfterExit == exits = 1 => fallbacks = 0
NoFallbackFalseSuccess == fallbackFailed => ~finished
EntryFailureFallbackUnreachable == ~(entryFailed /\ phase = "closed" /\ finished /\ fallbacks = 1)
ExitFailureReportedUnreachable == ~(exitFailed /\ phase = "closed" /\ finished /\ fallbacks = 0)
=============================================================================
