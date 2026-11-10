-------------------------- MODULE KnowledgeWorkflow --------------------------
EXTENDS Naturals, FiniteSets, TLC

(* One serialized turn. Generic graph token matching, provider accounting and
   the session transaction are interfaces, not reimplemented protocols here. *)
CONSTANTS MaxSteps, FailBusinessErrors, BoundSemanticRepair, ClearSourceContext,
          GuardInputCorrection

Nodes == {"plan", "tool", "compose", "verify", "repair", "deliver", "complete"}
Phases == {"source_policy", "source_validation", "before", "executing", "after",
           "runtime_completion", "post_runtime", "authority_check", "evidence_check", "final_check",
           "commit", "failure", "done"}
Statuses == {"none", "answered", "partial", "abstained", "clarification", "error"}
Completions == {"none", "complete", "partial", "conflict", "insufficient",
                "clarification", "error"}
Verdicts == {"none", "supported", "incomplete", "unsupported"}
EmptyEpoch == 3
EpochTags == 0..3
DerivedKeys == {"catalog", "offsets", "checkpoint", "last_answer"}
EmptyPayload == [answer |-> FALSE, claims |-> FALSE, evidence |-> FALSE]
EmptyDerived == [k \in DerivedKeys |-> EmptyEpoch]
ExecutionStops == {"cancel", "budget"}

VARIABLE s
vars == <<s>>

Init ==
    s \in [
        phase : {"source_policy"}, node : {"plan"}, nextNode : {"plan"},
        epoch : {0}, evidenceEpoch : {0},
        derived : {[k \in DerivedKeys |-> 0]}, workEpoch : {0},
        step : {0}, decision : {"answer"}, stop : {"none"},
        halted : {FALSE}, incompleteTool : {FALSE},
        repairEnabled : BOOLEAN, semanticRepairs : {0}, verdict : {"none"},
        inputCorrections : {0}, correctionSafe : {TRUE},
        status : {"none"}, completion : {"none"}, payload : {EmptyPayload},
        lineage : {FALSE}, derivedTrace : {FALSE},
        receipts : {0}, receiptsAtFailure : {0},
        signal : {"none"}, failureDetected : {FALSE}, graphDone : {FALSE},
        commitKind : {"none"}, promotedAnswer : {FALSE}, assistantEvent : {FALSE}
    ]

(* A policy change never retains old evidence. An auth-only change may keep
   pinned evidence only after successful revalidation, represented by a new
   freshness tag. Freshness tags are not document version numbers. *)
SourcePolicy(kind, keepPinned) ==
    /\ s.phase = "source_policy"
    /\ kind \in {"none", "policy", "authority"}
    /\ keepPinned \in BOOLEAN
    /\ keepPinned => kind = "authority"
    /\ s' = IF kind = "none"
        THEN [s EXCEPT !.phase = "source_validation"]
        ELSE [s EXCEPT
            !.phase = "source_validation",
            !.epoch = 1,
            !.derived = [k \in DerivedKeys |->
                IF k = "checkpoint" /\ ~ClearSourceContext THEN s.derived[k]
                ELSE EmptyEpoch],
            !.evidenceEpoch = IF keepPinned THEN 1 ELSE EmptyEpoch]

(* A second, ordinary evidence validation can fail even after a pinned carry.
   begin_work_state then binds newly active derived work to the resulting epoch. *)
ValidateSources(valid) ==
    /\ s.phase = "source_validation"
    /\ valid \in BOOLEAN
    /\ ~valid => s.evidenceEpoch # EmptyEpoch
    /\ LET invalid == ~valid
           epoch == IF invalid THEN s.epoch + 1 ELSE s.epoch
       IN s' = [s EXCEPT
           !.phase = "before", !.epoch = epoch, !.workEpoch = epoch,
           !.evidenceEpoch = IF invalid THEN EmptyEpoch ELSE @,
           !.derived = IF invalid THEN EmptyDerived ELSE @]

(* At most one external invalidation is represented. It may arrive after the
   last sampled check: this model deliberately does not assert atomic fencing
   of authorization/cancellation against the database commit. *)
Signal(reason) ==
    /\ s.phase # "done" /\ s.signal = "none"
    /\ reason \in {"cancel", "budget", "authority"}
    /\ s' = [s EXCEPT !.signal = reason]

DetectFailure ==
    s' = [s EXCEPT !.phase = "failure", !.failureDetected = TRUE,
                  !.receiptsAtFailure = s.receipts]

BeforeFailure ==
    /\ s.phase = "before" /\ s.signal # "none"
    /\ DetectFailure

BeginNode ==
    /\ s.phase = "before" /\ s.signal = "none"
    /\ s.node # "complete"
    /\ s.node = "repair" => s.semanticRepairs < 2
    /\ s' = [s EXCEPT !.phase = "executing",
        !.semanticRepairs = IF s.node = "repair" THEN @ + 1 ELSE @]

Plan(action) ==
    /\ s.phase = "executing" /\ s.node = "plan"
    /\ action \in {"answer", "clarify", "tool"}
    /\ s' = [s EXCEPT !.phase = "after", !.decision = action,
        !.nextNode = IF action = "tool" THEN "tool" ELSE "compose"]

PlanExplorationStop ==
    /\ s.phase = "executing" /\ s.node = "plan" /\ s.step > 0
    /\ s' = [s EXCEPT !.phase = "after", !.decision = "answer",
        !.stop = "exploration_budget", !.nextNode = "compose"]

ToolSuccess(incomplete) ==
    /\ s.phase = "executing" /\ s.node = "tool" /\ s.step < MaxSteps
    /\ incomplete \in BOOLEAN
    /\ s' = [s EXCEPT !.phase = "after", !.step = @ + 1,
        !.receipts = @ + 1, !.lineage = TRUE, !.derivedTrace = TRUE,
        !.evidenceEpoch = s.epoch, !.derived.catalog = s.epoch,
        !.derived.offsets = s.epoch, !.incompleteTool = @ \/ incomplete,
        !.stop = IF s.step + 1 = MaxSteps THEN "step_limit" ELSE @,
        !.nextNode = IF s.step + 1 = MaxSteps THEN "compose" ELSE "plan"]

(* A caught ordinary tool error is a normal graph result, not a graph throw.
   Earlier receipts/lineage remain; only the publication boundary redacts. *)
ToolFailure ==
    /\ s.phase = "executing" /\ s.node = "tool" /\ s.step < MaxSteps
    /\ s' = [s EXCEPT !.phase = "after", !.step = @ + 1,
        !.receipts = @ + 1, !.halted = TRUE, !.stop = "tool_failure",
        !.nextNode = "compose"]

ToolCorrection(eligible) ==
    /\ s.phase = "executing" /\ s.node = "tool"
    /\ s.step + 1 < MaxSteps
    /\ eligible \in BOOLEAN
    /\ (~GuardInputCorrection \/ (eligible /\ s.inputCorrections = 0))
    /\ s' = [s EXCEPT !.phase = "after", !.step = @ + 1,
        !.receipts = @ + 1, !.inputCorrections = @ + 1,
        !.correctionSafe = (@ /\ eligible), !.nextNode = "plan"]

RepeatedTool ==
    /\ s.phase = "executing" /\ s.node = "tool"
    /\ s.step > 0 /\ s.step < MaxSteps
    /\ s' = [s EXCEPT !.phase = "after", !.step = @ + 1,
        !.halted = TRUE, !.stop = "repeated_action", !.nextNode = "compose"]

Compose(mode, completion, verify) ==
    /\ s.phase = "executing" /\ s.node = "compose"
    /\ mode \in {"cited", "uncited", "abstained"}
    /\ completion \in {"complete", "partial", "conflict"}
    /\ verify \in BOOLEAN
    /\ mode = "cited" => s.evidenceEpoch # EmptyEpoch
    /\ LET status == CASE s.stop = "tool_failure" -> "error"
                       [] s.decision = "clarify" -> "clarification"
                       [] mode = "abstained" -> "abstained"
                       [] OTHER -> "answered"
           claims == status = "answered" /\ mode = "cited"
           verifyNow == verify /\ claims
       IN s' = [s EXCEPT !.phase = "after", !.status = status,
           !.completion = CASE status = "error" -> "error"
                            [] status = "clarification" -> "clarification"
                            [] status = "abstained" -> "insufficient"
                            [] OTHER -> completion,
           !.payload = [answer |-> TRUE, claims |-> claims, evidence |-> FALSE],
           !.lineage = @ \/ claims, !.derivedTrace = @ \/ claims,
           !.nextNode = IF verifyNow THEN "verify" ELSE "deliver"]

Verify(verdict) ==
    /\ s.phase = "executing" /\ s.node = "verify"
    /\ verdict \in Verdicts \ {"none"}
    /\ s' = [s EXCEPT !.phase = "after", !.verdict = verdict,
        !.derivedTrace = TRUE,
        !.nextNode = IF verdict = "unsupported" /\ s.repairEnabled
                           /\ (~BoundSemanticRepair \/ s.semanticRepairs = 0)
                    THEN "repair" ELSE "deliver"]

RepairSuccess(answered) ==
    /\ s.phase = "executing" /\ s.node = "repair"
    /\ answered \in BOOLEAN
    /\ s' = [s EXCEPT !.phase = "after", !.nextNode = "verify",
        !.receipts = @ + 1, !.lineage = TRUE, !.derivedTrace = TRUE,
        !.evidenceEpoch = s.epoch, !.derived.catalog = s.epoch,
        !.derived.offsets = s.epoch,
        !.status = IF answered THEN "answered" ELSE "abstained",
        !.completion = IF answered THEN "complete" ELSE "insufficient",
        !.payload = [answer |-> TRUE, claims |-> answered, evidence |-> FALSE]]

RepairFailure ==
    /\ s.phase = "executing" /\ s.node = "repair"
    /\ s' = [s EXCEPT !.phase = "failure", !.failureDetected = TRUE,
        !.receipts = @ + 1, !.receiptsAtFailure = s.receipts + 1]

Deliver ==
    /\ s.phase = "executing" /\ s.node = "deliver"
    /\ s.signal # "authority"
    /\ LET unsupported == s.verdict = "unsupported"
           status == IF unsupported THEN "abstained" ELSE s.status
           incomplete == s.verdict = "incomplete" \/ s.incompleteTool
                      \/ s.stop \in {"repeated_action", "step_limit", "exploration_budget"}
           completion == CASE status = "abstained" -> "insufficient"
                            [] status = "answered" /\ incomplete -> "partial"
                            [] OTHER -> s.completion
       IN s' = [s EXCEPT !.phase = "after", !.nextNode = "complete",
           !.status = IF status = "answered" /\ completion \in {"partial", "conflict"}
                      THEN "partial" ELSE status,
           !.completion = completion, !.payload.evidence = s.lineage,
           !.payload.claims = IF unsupported THEN FALSE ELSE @]

DeliveryAuthorityFailure ==
    /\ s.phase = "executing" /\ s.node = "deliver"
    /\ s.signal = "authority"
    /\ DetectFailure

(* Ordinary uncaught node/revalidation exceptions end the turn, with no replay.
   Tool errors use ToolFailure; repair can retain a failed receipt above. *)
NodeException ==
    /\ s.phase = "executing" /\ s.node # "tool"
    /\ DetectFailure

AfterFailure ==
    /\ s.phase = "after" /\ s.signal \in ExecutionStops
    /\ DetectFailure

(* Generic graph accept is abstracted as matching exactly the operation just
   executed. graphDone means native route terminality, not Runtime returning:
   next/finish/program-close may still throw in runtime_completion below. *)
AcceptNode ==
    /\ s.phase = "after" /\ s.signal \notin ExecutionStops
    /\ s' = [s EXCEPT !.node = s.nextNode,
        !.phase = IF s.nextNode = "complete" THEN "runtime_completion" ELSE "before",
        !.graphDone = s.nextNode = "complete"]

RuntimeCompletionPass ==
    /\ s.phase = "runtime_completion" /\ s.signal \notin ExecutionStops
    /\ s' = [s EXCEPT !.phase = "post_runtime"]

RuntimeCompletionFailure ==
    /\ s.phase = "runtime_completion"
    /\ DetectFailure

PostRuntimeFailure ==
    /\ s.phase = "post_runtime"
    /\ s.signal \in ExecutionStops \/ (FailBusinessErrors /\ s.status = "error")
    /\ DetectFailure

PostRuntimePass ==
    /\ s.phase = "post_runtime"
    /\ s.signal \notin ExecutionStops
    /\ ~(FailBusinessErrors /\ s.status = "error")
    /\ s' = [s EXCEPT !.phase = "authority_check"]

AuthorityFailure ==
    /\ s.phase = "authority_check" /\ s.signal = "authority"
    /\ DetectFailure

AuthorityPass ==
    /\ s.phase = "authority_check" /\ s.signal # "authority"
    /\ s' = [s EXCEPT !.phase = "evidence_check"]

RevalidatePass ==
    /\ s.phase = "evidence_check"
    /\ s' = [s EXCEPT !.phase = "final_check"]

RevalidateFailure ==
    /\ s.phase = "evidence_check"
    /\ DetectFailure

FinalCheckFailure ==
    /\ s.phase = "final_check" /\ s.signal \in ExecutionStops
    /\ DetectFailure

FinalCheckPass ==
    /\ s.phase = "final_check" /\ s.signal \notin ExecutionStops
    /\ s' = [s EXCEPT !.phase = "commit"]

(* Each store operation is an abstract atomic durable result. Actual SQL
   transaction, writer fencing, commit faults and recovery are separate owners. *)
Finish ==
    /\ s.phase = "commit"
    /\ s' = [s EXCEPT !.phase = "done", !.commitKind = "finish",
        !.promotedAnswer = TRUE, !.assistantEvent = TRUE,
        !.derived.last_answer = s.epoch]

Fail ==
    /\ s.phase = "failure"
    /\ s' = [s EXCEPT !.phase = "done", !.commitKind = "fail",
        !.status = "error", !.completion = "error", !.payload = EmptyPayload,
        !.derivedTrace = FALSE]

Next ==
    \/ \E kind \in {"none", "policy", "authority"}, keep \in BOOLEAN:
           SourcePolicy(kind, keep)
    \/ \E valid \in BOOLEAN: ValidateSources(valid)
    \/ \E reason \in {"cancel", "budget", "authority"}: Signal(reason)
    \/ BeforeFailure \/ BeginNode
    \/ \E action \in {"answer", "clarify", "tool"}: Plan(action)
    \/ PlanExplorationStop
    \/ \E incomplete \in BOOLEAN: ToolSuccess(incomplete)
    \/ ToolFailure \/ RepeatedTool
    \/ \E eligible \in BOOLEAN: ToolCorrection(eligible)
    \/ \E mode \in {"cited", "uncited", "abstained"},
          completion \in {"complete", "partial", "conflict"}, verify \in BOOLEAN:
           Compose(mode, completion, verify)
    \/ \E verdict \in Verdicts \ {"none"}: Verify(verdict)
    \/ \E answered \in BOOLEAN: RepairSuccess(answered)
    \/ RepairFailure \/ Deliver \/ DeliveryAuthorityFailure \/ NodeException
    \/ AfterFailure \/ AcceptNode \/ RuntimeCompletionPass \/ RuntimeCompletionFailure
    \/ PostRuntimeFailure \/ PostRuntimePass
    \/ AuthorityFailure \/ AuthorityPass \/ RevalidatePass \/ RevalidateFailure
    \/ FinalCheckFailure \/ FinalCheckPass \/ Finish \/ Fail

Spec == Init /\ [][Next]_vars

TypeOK ==
    s \in [
        phase : Phases, node : Nodes, nextNode : Nodes,
        epoch : 0..2, evidenceEpoch : EpochTags,
        derived : [DerivedKeys -> EpochTags], workEpoch : 0..2,
        step : 0..MaxSteps, decision : {"answer", "clarify", "tool"},
        stop : {"none", "tool_failure", "step_limit", "repeated_action", "exploration_budget"},
        halted : BOOLEAN, incompleteTool : BOOLEAN,
        repairEnabled : BOOLEAN, semanticRepairs : 0..2, verdict : Verdicts,
        inputCorrections : 0..MaxSteps, correctionSafe : BOOLEAN,
        status : Statuses, completion : Completions,
        payload : [answer : BOOLEAN, claims : BOOLEAN, evidence : BOOLEAN],
        lineage : BOOLEAN, derivedTrace : BOOLEAN,
        receipts : 0..(MaxSteps + 2), receiptsAtFailure : 0..(MaxSteps + 2),
        signal : {"none", "cancel", "budget", "authority"},
        failureDetected : BOOLEAN, graphDone : BOOLEAN,
        commitKind : {"none", "finish", "fail"},
        promotedAnswer : BOOLEAN, assistantEvent : BOOLEAN
    ]

TerminalCommitConsistent ==
    /\ (s.phase = "done") = (s.commitKind # "none")
    /\ (s.commitKind = "finish" => s.graphDone)
    /\ (s.graphDone => s.node = "complete")
    /\ (s.commitKind = "fail" => s.status = "error" /\ s.completion = "error")
    /\ (s.commitKind = "finish" /\ s.status = "partial" =>
           s.completion \in {"partial", "conflict"})
    /\ (s.commitKind = "finish" /\ s.status = "abstained" =>
           s.completion = "insufficient")
    /\ (s.commitKind = "finish" /\ s.status = "clarification" =>
           s.completion = "clarification")

FailedBusinessUsesRedactedFail ==
    s.phase = "done" /\ s.status = "error" =>
        s.commitKind = "fail" /\ s.payload = EmptyPayload /\ ~s.derivedTrace

NoFailedAnswerPromotion ==
    s.phase = "done" /\ s.status = "error" =>
        ~s.promotedAnswer /\ ~s.assistantEvent

SingleSemanticRepair == s.semanticRepairs <= 1
SingleInputCorrection == s.inputCorrections <= 1
NoUnsafeInputCorrection == s.correctionSafe
NoInputCorrectionWitness == s.inputCorrections = 0

SourceEpochClearsDerivedContext ==
    /\ \A k \in DerivedKeys: s.derived[k] \in {EmptyEpoch, s.epoch}
    /\ s.evidenceEpoch \in {EmptyEpoch, s.epoch}

CurrentWorkEpoch ==
    s.phase \notin {"source_policy", "source_validation"} => s.workEpoch = s.epoch

UnsupportedDoesNotAnswer ==
    s.commitKind = "finish" /\ s.verdict = "unsupported" =>
        s.status = "abstained" /\ ~s.payload.claims

ExplorationBound ==
    /\ s.step <= MaxSteps
    /\ (s.phase = "before" /\ s.step = MaxSteps => s.node \notin {"plan", "tool"})
    /\ (s.phase = "before" /\ s.halted => s.node \notin {"plan", "tool"})
    /\ (s.stop = "tool_failure" => s.node \notin {"verify", "repair"})

ReceiptsSurviveFailure ==
    s.commitKind = "fail" => s.receipts >= s.receiptsAtFailure

ObservedStopCannotFinish ==
    s.failureDetected => s.commitKind # "finish"
=============================================================================
