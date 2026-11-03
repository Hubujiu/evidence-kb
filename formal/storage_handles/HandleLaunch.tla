----------------------------- MODULE HandleLaunch -----------------------------
EXTENDS Naturals
CONSTANTS RetireFailedLaunch, GuardUnstartedClose, FenceStoppedLaunch, RetainTimedOutWorker
VARIABLES launched, registered, stopped, closed, ready, workerDone, launchFailed,
          managerEntered, managerExited, outcome, reports, joinedUnstarted,
          lateLaunch, timedOut
vars == <<launched, registered, stopped, closed, ready, workerDone, launchFailed,
          managerEntered, managerExited, outcome, reports, joinedUnstarted,
          lateLaunch, timedOut>>
Init == /\ launched = FALSE /\ registered = TRUE /\ stopped = FALSE
        /\ closed = FALSE /\ ready = FALSE /\ workerDone = FALSE
        /\ launchFailed = FALSE /\ managerEntered = FALSE /\ managerExited = FALSE
        /\ outcome = "none" /\ reports = 0 /\ joinedUnstarted = FALSE
        /\ lateLaunch = FALSE /\ timedOut = FALSE
CanLaunch == ~launched /\ ~launchFailed /\ (~FenceStoppedLaunch \/ ~stopped)
Launch == /\ CanLaunch /\ launched' = TRUE /\ lateLaunch' = closed
          /\ UNCHANGED <<registered, stopped, closed, ready, workerDone, launchFailed,
                         managerEntered, managerExited, outcome, reports, joinedUnstarted, timedOut>>
LaunchFailure == /\ CanLaunch /\ launchFailed' = TRUE
                 /\ registered' = IF RetireFailedLaunch THEN FALSE ELSE registered
                 /\ stopped' = IF RetireFailedLaunch THEN TRUE ELSE stopped
                 /\ outcome' = IF RetireFailedLaunch THEN "UNKNOWN" ELSE outcome
                 /\ reports' = IF RetireFailedLaunch THEN reports + 1 ELSE reports
                 /\ UNCHANGED <<launched, closed, ready, workerDone, managerEntered,
                                managerExited, joinedUnstarted, lateLaunch, timedOut>>
CloseUnstarted == /\ ~launched /\ ~closed
                  /\ closed' = TRUE /\ stopped' = TRUE
                  /\ joinedUnstarted' = joinedUnstarted
                  /\ registered' = IF GuardUnstartedClose THEN FALSE ELSE registered
                  /\ outcome' = IF GuardUnstartedClose THEN "UNKNOWN" ELSE outcome
                  /\ reports' = IF GuardUnstartedClose /\ ~stopped THEN reports + 1 ELSE reports
                  /\ UNCHANGED <<launched, ready, workerDone, launchFailed, managerEntered,
                                 managerExited, lateLaunch, timedOut>>
RetryUnstartedClose == /\ ~launched /\ closed /\ ~GuardUnstartedClose /\ ~joinedUnstarted
                       /\ joinedUnstarted' = TRUE
                       /\ UNCHANGED <<launched, registered, stopped, closed, ready, workerDone,
                                      launchFailed, managerEntered, managerExited, outcome, reports,
                                      lateLaunch, timedOut>>
CloseLaunched == /\ launched /\ ~closed /\ closed' = TRUE /\ stopped' = TRUE
                 /\ UNCHANGED <<launched, registered, ready, workerDone, launchFailed,
                                managerEntered, managerExited, outcome, reports, joinedUnstarted,
                                lateLaunch, timedOut>>
ReadyTimeout == /\ launched /\ ~ready /\ ~workerDone /\ ~timedOut
                /\ timedOut' = TRUE /\ stopped' = TRUE
                /\ registered' = IF RetainTimedOutWorker THEN registered ELSE FALSE
                /\ UNCHANGED <<launched, closed, ready, workerDone, launchFailed,
                               managerEntered, managerExited, outcome, reports,
                               joinedUnstarted, lateLaunch>>
Enter == /\ launched /\ ~managerEntered /\ ~workerDone
         /\ managerEntered' = TRUE /\ ready' = TRUE
         /\ UNCHANGED <<launched, registered, stopped, closed, workerDone, launchFailed,
                        managerExited, outcome, reports, joinedUnstarted, lateLaunch, timedOut>>
EnterError == /\ launched /\ ~managerEntered /\ ~workerDone
              /\ workerDone' = TRUE /\ stopped' = TRUE /\ registered' = FALSE
              /\ outcome' = "UNKNOWN" /\ reports' = reports + 1
              /\ UNCHANGED <<launched, closed, ready, launchFailed, managerEntered,
                             managerExited, joinedUnstarted, lateLaunch, timedOut>>
Finish == /\ managerEntered /\ ~workerDone
          /\ managerExited' = TRUE /\ workerDone' = TRUE /\ stopped' = TRUE
          /\ registered' = FALSE /\ reports' = reports + 1
          /\ outcome' \in IF stopped THEN {"ROLLED_BACK", "UNKNOWN"}
                          ELSE {"COMMITTED", "ROLLED_BACK", "UNKNOWN"}
          /\ UNCHANGED <<launched, closed, ready, launchFailed, managerEntered,
                         joinedUnstarted, lateLaunch, timedOut>>
Next == Launch \/ LaunchFailure \/ CloseUnstarted \/ RetryUnstartedClose \/ CloseLaunched \/ ReadyTimeout \/ Enter \/ EnterError \/ Finish
Spec == Init /\ [][Next]_vars
TypeOK == /\ launched \in BOOLEAN /\ registered \in BOOLEAN /\ stopped \in BOOLEAN
          /\ closed \in BOOLEAN /\ ready \in BOOLEAN /\ workerDone \in BOOLEAN
          /\ launchFailed \in BOOLEAN /\ managerEntered \in BOOLEAN /\ managerExited \in BOOLEAN
          /\ joinedUnstarted \in BOOLEAN /\ lateLaunch \in BOOLEAN /\ timedOut \in BOOLEAN
          /\ outcome \in {"none", "UNKNOWN", "COMMITTED", "ROLLED_BACK"} /\ reports \in 0..2
FailedLaunchReleases == launchFailed => ~registered
NoUnstartedJoin == ~joinedUnstarted
NoLateLaunch == ~lateLaunch
LaunchedWorkerRetainsSlot == launched /\ ~workerDone => registered
OneTerminalReport == reports <= 1
KnownOutcomeNeedsExit == outcome \in {"COMMITTED", "ROLLED_BACK"} => managerExited
NoFreshCommit == ~(outcome = "COMMITTED" /\ workerDone)
NoCancelledUnstarted == ~(closed /\ ~launched /\ reports = 1 /\ ~registered)
NoLateStartupCleanup == ~(timedOut /\ managerExited /\ outcome = "ROLLED_BACK" /\ ~registered)
===============================================================================
