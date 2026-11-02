-------------------------- MODULE AgentConsoleDraft --------------------------
EXTENDS Naturals, Sequences, TLC

CONSTANTS ClearPolicy, CaptureBeforeCreate
Texts == {"submitted", "different", ""}
Pending == {"create-await", "create-refresh-await", "turn-await", "session-await"}
Phases == Pending \cup {"idle", "clear-ready", "complete", "discarded", "failed"}
NoOwner == 99
Owners == (0..4) \cup {NoOwner}

VARIABLES phase, implicit, scope, uiRevision, draft, question, inputs,
          submitOwner, submitText, submitScope, capturedOwner, lastCommit
vars == <<phase, implicit, scope, uiRevision, draft, question, inputs,
          submitOwner, submitText, submitScope, capturedOwner, lastCommit>>

EmptyCommit == [attempted |-> FALSE, cleared |-> FALSE, owner |-> NoOwner,
                atDraft |-> NoOwner, atScope |-> 0, before |-> ""]
CurrentScope == submitScope = scope
ClearAllowed == CASE ClearPolicy = "unconditional" -> TRUE
                  [] ClearPolicy = "text" -> question = submitText
                  [] OTHER -> capturedOwner = draft

Init ==
    /\ phase = "idle" /\ implicit \in BOOLEAN
    /\ scope = 0 /\ uiRevision = 0 /\ draft = 0 /\ question = "submitted"
    /\ inputs = <<>>
    /\ submitOwner = NoOwner /\ capturedOwner = NoOwner
    /\ submitText = "" /\ submitScope = 0 /\ lastCommit = EmptyCommit

Submit ==
    /\ phase = "idle"
    /\ phase' = IF implicit THEN "create-await" ELSE "turn-await"
    /\ submitOwner' = draft /\ submitText' = question /\ submitScope' = scope
    /\ capturedOwner' = IF CaptureBeforeCreate \/ ~implicit THEN draft ELSE NoOwner
    /\ UNCHANGED <<implicit, scope, uiRevision, draft, question, inputs, lastCommit>>

Input(value) ==
    /\ phase \in Pending /\ Len(inputs) < 2
    /\ question' = value /\ draft' = draft + 1
    /\ inputs' = Append(inputs, [at |-> phase, text |-> value])
    /\ UNCHANGED <<phase, implicit, scope, uiRevision, submitOwner, submitText,
                   submitScope, capturedOwner, lastCommit>>

ResetScope ==
    /\ phase \in Pending /\ scope = 0
    /\ scope' = 1 /\ uiRevision' = uiRevision + 1
    /\ draft' = draft + 1 /\ question' = ""
    /\ UNCHANGED <<phase, implicit, inputs, submitOwner, submitText, submitScope,
                   capturedOwner, lastCommit>>

CreateResponse ==
    /\ phase = "create-await"
    /\ phase' = IF CurrentScope THEN "create-refresh-await" ELSE "discarded"
    /\ uiRevision' = IF CurrentScope THEN uiRevision + 1 ELSE uiRevision
    \* Installing the newly created session is NOT a question input event.
    /\ UNCHANGED <<implicit, scope, draft, question, inputs, submitOwner,
                   submitText, submitScope, capturedOwner, lastCommit>>

CreateRefreshResponse ==
    /\ phase = "create-refresh-await"
    /\ phase' = IF CurrentScope THEN "turn-await" ELSE "discarded"
    /\ capturedOwner' = IF CurrentScope /\ ~CaptureBeforeCreate
                         THEN draft ELSE capturedOwner
    /\ UNCHANGED <<implicit, scope, uiRevision, draft, question, inputs,
                   submitOwner, submitText, submitScope, lastCommit>>

TurnResponse ==
    /\ phase = "turn-await"
    /\ phase' = IF CurrentScope THEN "session-await" ELSE "discarded"
    /\ UNCHANGED <<implicit, scope, uiRevision, draft, question, inputs,
                   submitOwner, submitText, submitScope, capturedOwner, lastCommit>>

SessionResponse ==
    /\ phase = "session-await"
    /\ phase' = IF CurrentScope THEN "clear-ready" ELSE "discarded"
    /\ UNCHANGED <<implicit, scope, uiRevision, draft, question, inputs,
                   submitOwner, submitText, submitScope, capturedOwner, lastCommit>>

ClearCommit ==
    /\ phase = "clear-ready"
    /\ phase' = "complete"
    /\ question' = IF ClearAllowed THEN "" ELSE question
    /\ draft' = IF ClearAllowed THEN draft + 1 ELSE draft
    /\ lastCommit' = [attempted |-> TRUE, cleared |-> ClearAllowed,
                       owner |-> submitOwner, atDraft |-> draft,
                       atScope |-> scope, before |-> question]
    \* clearDraft explicitly rotates ownership; assigning value does not emit input.
    /\ UNCHANGED <<implicit, scope, uiRevision, inputs, submitOwner,
                   submitText, submitScope, capturedOwner>>

Reject ==
    /\ phase \in Pending
    /\ phase' = IF CurrentScope THEN "failed" ELSE "discarded"
    /\ UNCHANGED <<implicit, scope, uiRevision, draft, question, inputs,
                   submitOwner, submitText, submitScope, capturedOwner, lastCommit>>

Next == Submit \/ (\E value \in Texts : Input(value)) \/ ResetScope \/
        CreateResponse \/ CreateRefreshResponse \/ TurnResponse \/
        SessionResponse \/ ClearCommit \/ Reject
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in Phases /\ implicit \in BOOLEAN
    /\ scope \in 0..1 /\ uiRevision \in 0..2 /\ draft \in 0..4
    /\ question \in Texts /\ inputs \in Seq([at : Pending, text : Texts])
    /\ Len(inputs) <= 2
    /\ submitOwner \in Owners /\ capturedOwner \in Owners
    /\ submitText \in Texts /\ submitScope \in 0..1
    /\ lastCommit \in [attempted : BOOLEAN, cleared : BOOLEAN, owner : Owners,
                        atDraft : Owners, atScope : 0..1, before : Texts]
NoStaleDraftClear == lastCommit.cleared =>
    (lastCommit.owner = lastCommit.atDraft /\ submitScope = lastCommit.atScope)
NoFailureClear == phase \in {"failed", "discarded"} => ~lastCommit.attempted
CurrentDraftClears == phase = "complete" /\ lastCommit.atDraft = submitOwner =>
    (lastCommit.cleared /\ question = "")
NewerDraftPreserved == phase = "complete" /\ lastCommit.atDraft # submitOwner =>
    (~lastCommit.cleared /\ question = lastCommit.before)
=============================================================================
