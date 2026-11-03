--------------------------- MODULE PythonLease ---------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS SetupFinally, LastUserOnly, RemoveSignal, JoinTimer
VARIABLES phase, users, native, token, signal, timer, settled, badRelease, failedSetup
vars == <<phase,users,native,token,signal,timer,settled,badRelease,failedSetup>>
Init == /\ phase = "idle" /\ users = 0 /\ native = FALSE /\ token = FALSE
        /\ signal = FALSE /\ timer = "absent" /\ settled = 0
        /\ badRelease = FALSE /\ failedSetup = FALSE
Acquire == /\ phase = "idle" /\ phase' = "setup" /\ native' = TRUE /\ users' = 1
           /\ UNCHANGED <<token,signal,timer,settled,badRelease,failedSetup>>
Setup(ok) == /\ phase = "setup" /\ ok \in BOOLEAN
             /\ phase' = (IF ok THEN "body" ELSE IF SetupFinally THEN "release" ELSE "done")
             /\ token' = ok /\ failedSetup' = ~ok
             /\ UNCHANGED <<users,native,signal,timer,settled,badRelease>>
Nest == /\ phase = "body" /\ users = 1 /\ phase' = "nested" /\ users' = 2
        /\ UNCHANGED <<native,token,signal,timer,settled,badRelease,failedSetup>>
NestedExit == /\ phase = "nested" /\ phase' = "body-end" /\ users' = 1
              /\ native' = (IF LastUserOnly THEN native ELSE FALSE)
              /\ badRelease' = ~LastUserOnly
              /\ UNCHANGED <<token,signal,timer,settled,failedSetup>>
Invoke == /\ phase \in {"body","body-end"} /\ phase' = "invoking"
          /\ signal' = TRUE /\ timer' = "started"
          /\ UNCHANGED <<users,native,token,settled,badRelease,failedSetup>>
TimerFire == /\ phase = "invoking" /\ timer = "started" /\ timer' = "callback"
             /\ UNCHANGED <<phase,users,native,token,signal,settled,badRelease,failedSetup>>
TimerDone == /\ timer = "callback" /\ timer' = "stopped"
             /\ UNCHANGED <<phase,users,native,token,signal,settled,badRelease,failedSetup>>
Finish == /\ phase \in {"body","body-end","invoking"}
          /\ (JoinTimer => timer # "callback")
          /\ phase' = "release" /\ token' = FALSE
          /\ signal' = (IF RemoveSignal THEN FALSE ELSE signal)
          /\ timer' = (IF timer = "callback" THEN timer ELSE "stopped")
          /\ UNCHANGED <<users,native,settled,badRelease,failedSetup>>
Release == /\ phase = "release" /\ phase' = "done" /\ users' = 0
           /\ native' = FALSE /\ settled' = settled + 1
           /\ UNCHANGED <<token,signal,timer,badRelease,failedSetup>>
Next == Acquire \/ (\E ok \in BOOLEAN : Setup(ok)) \/ Nest \/ NestedExit \/ Invoke
        \/ TimerFire \/ TimerDone \/ Finish \/ Release
Spec == Init /\ [][Next]_vars
TypeOK == /\ users \in 0..2 /\ settled \in 0..1
          /\ phase \in {"idle","setup","body","nested","body-end","invoking","release","done"}
          /\ timer \in {"absent","started","callback","stopped"}
          /\ {native,token,signal,badRelease,failedSetup} \subseteq BOOLEAN
NoSetupLeak == phase = "done" => ~native /\ users = 0
NoEarlyRelease == ~badRelease
NoSignalLeak == phase = "done" => ~signal
NoLiveTimerAfterReturn == phase = "done" => timer # "callback"
SetupFailureRecoveredUnreachable == ~(phase = "done" /\ failedSetup /\ settled = 1 /\ ~native)
NestedInvocationSettledUnreachable == ~(phase = "done" /\ ~failedSetup /\ timer = "stopped" /\ settled = 1)
=============================================================================
