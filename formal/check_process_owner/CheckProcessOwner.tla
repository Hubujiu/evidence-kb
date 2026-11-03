------------------------- MODULE CheckProcessOwner -------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Platform, DirectOnly, EarlyResume, StaleHandle, OverwriteFailure
ASSUME Platform = "linux"
Owned == {"direct", "grandchild"}
Processes == Owned \cup {"sentinel", "reused"}
Pid(p) == CASE p = "direct" -> 10 [] p = "grandchild" -> 11
           [] p = "sentinel" -> 12 [] OTHER -> 11
Generation(p) == IF p = "reused" THEN 2 ELSE 1
VARIABLE s
vars == <<s>>
Init == s = [phase |-> "new", alive |-> {"sentinel"}, born |-> {},
             parent |-> [p \in Processes |-> "outside"], zombies |-> {},
             contained |-> FALSE, resumed |-> FALSE,
             escaped |-> FALSE, handles |-> {}, signals |-> {}, refused |-> {},
             childExit |-> "pending", timedOut |-> FALSE, cancelled |-> FALSE,
             failed |-> FALSE, cleanup |-> "pending", verdict |-> "pending"]
Launch == /\ s.phase = "new"
          /\ s' = [s EXCEPT !.phase = "suspended", !.alive = @ \cup {"direct"},
                    !.born = {"direct"}, !.parent["direct"] = "owner"]
Contain == /\ s.phase = "suspended" /\ ~s.contained
           /\ s' = [s EXCEPT !.contained = TRUE]
Resume == /\ s.phase = "suspended" /\ (s.contained \/ EarlyResume)
          /\ s' = [s EXCEPT !.phase = "running", !.resumed = TRUE]
Fork == /\ s.phase = "running" /\ "direct" \in s.alive
        /\ "grandchild" \notin s.born
        /\ s' = [s EXCEPT !.alive = @ \cup {"grandchild"},
                  !.born = @ \cup {"grandchild"}, !.parent["grandchild"] = "direct"]
EscapeSession == /\ "grandchild" \in s.alive /\ ~s.escaped
                 /\ s' = [s EXCEPT !.escaped = TRUE]
ExitDirect(code) ==
    /\ s.phase = "running" /\ "direct" \in s.alive
    /\ s' = [s EXCEPT !.alive = @ \ {"direct"}, !.zombies = @ \cup {"direct"},
              !.childExit = code,
              !.parent["grandchild"] = IF "grandchild" \in s.born
                   THEN "owner" ELSE @]
ExitGrandchild ==
    /\ "grandchild" \in s.alive
    /\ s' = [s EXCEPT !.alive = @ \ {"grandchild"},
              !.zombies = @ \cup {"grandchild"}]
Timeout == /\ s.phase = "running" /\ ~s.timedOut
           /\ s' = [s EXCEPT !.timedOut = TRUE, !.failed = TRUE]
Cancel == /\ s.phase = "running" /\ ~s.cancelled
          /\ s' = [s EXCEPT !.cancelled = TRUE, !.failed = TRUE]
BeginCleanup ==
    /\ s.phase = "running"
    /\ (s.childExit # "pending" \/ s.timedOut \/ s.cancelled)
    /\ s' = [s EXCEPT !.phase = "cleaning"]
\* Linux obtains pidfds only for owned children of the dedicated subreaper.
\* Adoption exposes grandchildren even after setsid; session IDs are irrelevant.
Capture(p) ==
    /\ s.phase = "cleaning" /\ p \in s.alive \cap Owned
    /\ s.parent[p] = "owner"
    /\ <<p, Generation(p)>> \notin s.handles
    /\ s' = [s EXCEPT !.handles = @ \cup {<<p, Generation(p)>>}]
Signal(h, p) ==
    /\ s.phase = "cleaning" /\ h \in s.handles /\ p \in s.alive
    /\ Pid(p) = Pid(h[1])
    /\ (Generation(p) = h[2] \/ StaleHandle)
    /\ (~DirectOnly \/ h[1] = "direct")
    /\ s' = [s EXCEPT !.alive = @ \ {p},
              !.signals = @ \cup {<<p, h[2], Generation(p)>>},
              !.zombies = IF p \in Owned THEN @ \cup {p} ELSE @,
              !.childExit = IF p = "direct" THEN "nonzero" ELSE @,
              !.parent["grandchild"] = IF p = "direct" /\ "grandchild" \in s.born
                   THEN "owner" ELSE @]
RefuseStale(h, p) ==
    /\ s.phase = "cleaning" /\ h \in s.handles /\ p \in s.alive
    /\ Pid(p) = Pid(h[1]) /\ Generation(p) # h[2]
    /\ ~StaleHandle /\ p \notin s.refused
    /\ s' = [s EXCEPT !.refused = @ \cup {p}]
Reap(p) ==
    /\ s.phase = "cleaning" /\ p \in s.zombies
    /\ s.parent[p] = "owner"
    /\ s' = [s EXCEPT !.zombies = @ \ {p}]
ReusePid ==
    /\ "grandchild" \in s.born /\ "grandchild" \notin s.alive \cup s.zombies
    /\ "reused" \notin s.born
    /\ s' = [s EXCEPT !.alive = @ \cup {"reused"}, !.born = @ \cup {"reused"}]
\* ECHILD is separate from sending termination.
ObserveEmpty ==
    /\ s.phase = "cleaning" /\ s.cleanup = "pending"
    /\ (IF DirectOnly THEN "direct" \notin s.alive \cup s.zombies
         ELSE (s.alive \cup s.zombies) \cap Owned = {})
    /\ s' = [s EXCEPT !.cleanup = "complete"]
CleanupError == /\ s.phase = "cleaning" /\ s.cleanup = "pending"
                /\ s' = [s EXCEPT !.cleanup = "failed", !.failed = TRUE]
Publish ==
    /\ s.phase = "cleaning" /\ s.cleanup # "pending"
    /\ LET failure == IF OverwriteFailure THEN s.childExit # "zero"
                      ELSE s.failed \/ s.childExit # "zero"
       IN s' = [s EXCEPT !.phase = "done", !.failed = failure,
                !.verdict = IF ~failure /\ s.cleanup = "complete"
                            THEN "PASS" ELSE "FAIL"]
Next == Launch \/ Contain \/ Resume \/ Fork \/ EscapeSession
        \/ (\E c \in {"zero", "nonzero"}: ExitDirect(c)) \/ ExitGrandchild
        \/ Timeout \/ Cancel \/ BeginCleanup \/ (\E p \in Owned: Capture(p))
        \/ (\E h \in s.handles, p \in Processes: Signal(h, p))
        \/ (\E h \in s.handles, p \in Processes: RefuseStale(h, p))
        \/ (\E p \in Owned: Reap(p)) \/ ReusePid \/ ObserveEmpty
        \/ CleanupError \/ Publish
Spec == Init /\ [][Next]_vars
TypeOK == /\ s.alive \subseteq Processes /\ s.zombies \subseteq Owned
          /\ s.refused \subseteq Processes
          /\ s.born \subseteq Processes
          /\ s.parent \in [Processes -> {"outside", "owner", "direct"}]
          /\ s.phase \in {"new", "suspended", "running", "cleaning", "done"}
          /\ s.childExit \in {"pending", "zero", "nonzero"}
          /\ s.cleanup \in {"pending", "complete", "failed"}
          /\ s.verdict \in {"pending", "PASS", "FAIL"}
          /\ s.handles \subseteq (Owned \X {1})
          /\ s.signals \subseteq (Processes \X {1} \X {1, 2})
          /\ \A b \in {s.contained, s.resumed, s.escaped, s.timedOut,
                        s.cancelled, s.failed}: b \in BOOLEAN
NoUnownedKill == \A hit \in s.signals: hit[1] \in Owned
NoPidReuseKill == \A hit \in s.signals: hit[2] = hit[3]
NoResumeBeforeContainment == s.resumed => s.contained
CleanupCompleteMeansEmpty == s.cleanup = "complete" => s.alive \cap Owned = {}
PassRequiresEvidence == s.verdict = "PASS" =>
    /\ s.childExit = "zero" /\ ~s.timedOut /\ ~s.cancelled /\ ~s.failed
    /\ s.alive \cap Owned = {} /\ s.zombies = {} /\ s.cleanup = "complete"
StickyFailure == (s.timedOut \/ s.cancelled) => s.failed
\* Reachability witnesses: TLC must violate these in separate coverage configs.
NeverPass == s.verdict # "PASS"
NeverAdoptEscape == ~(s.escaped /\ s.parent["grandchild"] = "owner"
                      /\ "grandchild" \in s.alive)
NeverCleanupError == s.cleanup # "failed"
NeverReusedHandle == ~("reused" \in s.alive /\ <<"grandchild", 1>> \in s.handles)
NeverTimeoutCleanup == ~(s.timedOut /\ s.childExit = "zero"
                         /\ s.verdict = "FAIL" /\ s.cleanup = "complete")
NeverEscapedCleanup == ~(s.escaped /\ s.cleanup = "complete" /\ s.verdict = "PASS"
                         /\ <<"grandchild", 1, 1>> \in s.signals)
NeverGenerationRefusal == ~("reused" \in s.refused /\ "reused" \in s.alive)
NeverCancelledCleanup == ~(s.cancelled /\ s.childExit = "zero"
                           /\ s.verdict = "FAIL" /\ s.cleanup = "complete")
=============================================================================
