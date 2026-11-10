--------------------------- MODULE WebRequestRecovery ---------------------------
EXTENDS Naturals, TLC

CONSTANTS ReleaseUnknown, PreserveReceipt, GuardDraft, GuardView, IsolateParked
Phases == {"idle", "creating", "submitting", "unconfirmed", "checking",
           "confirmed", "refreshing", "refresh_required", "complete", "next"}
Statuses == {"none", "unknown", "running", "not_found", "interrupted", "reconciliation_required"}

VARIABLES phase, implicit, started, requestId, posts, sent, known, status,
          view, draftOwner, submittedDraft, draftCleared, clearedOwner, renderView,
          renderAttempted, lastReadId, reads, parking
vars == <<phase, implicit, started, requestId, posts, sent, known, status,
          view, draftOwner, submittedDraft, draftCleared, clearedOwner, renderView,
          renderAttempted, lastReadId, reads, parking>>

Init ==
    /\ phase = "idle" /\ implicit \in BOOLEAN /\ started = FALSE
    /\ requestId = 0 /\ posts = 0 /\ sent = FALSE /\ known = FALSE /\ status = "none"
    /\ view = 0 /\ draftOwner = 0 /\ submittedDraft = 0
    /\ draftCleared = FALSE /\ clearedOwner = 0 /\ renderView = 0
    /\ renderAttempted = FALSE /\ lastReadId = 0 /\ reads = 0
    /\ parking = [retained |-> FALSE, started |-> FALSE, request |-> 0, session |-> 0]

Start ==
    /\ phase = "idle" /\ ~started
    /\ phase' = IF implicit THEN "creating" ELSE "submitting"
    /\ started' = TRUE /\ requestId' = 1 /\ submittedDraft' = draftOwner
    /\ UNCHANGED <<implicit, posts, sent, known, status, view, draftOwner,
                   draftCleared, clearedOwner, renderView, renderAttempted, lastReadId, reads, parking>>

CreateSuccess ==
    /\ phase = "creating" /\ phase' = "submitting"
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, status,
                   view, draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

CreateFailure ==
    /\ phase = "creating" /\ phase' = "idle"
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, status,
                   view, draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

Post ==
    /\ phase = "submitting" /\ ~sent
    /\ posts' = 1 /\ sent' = TRUE
    /\ UNCHANGED <<phase, implicit, started, requestId, known, status, view,
                   draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

LoseResponse ==
    /\ phase = "submitting" /\ sent
    /\ phase' = "unconfirmed" /\ status' = "unknown"
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, view,
                   draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

Check ==
    /\ phase = "unconfirmed" /\ reads < 2
    /\ phase' = "checking" /\ reads' = reads + 1 /\ lastReadId' = requestId
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, status,
                   view, draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, parking>>

Observe(value) ==
    /\ phase = "checking"
    /\ phase' = IF ReleaseUnknown THEN "complete" ELSE "unconfirmed"
    /\ status' = value
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, view,
                   draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

Receive ==
    /\ phase \in {"submitting", "checking"} /\ sent
    /\ phase' = "confirmed" /\ known' = TRUE /\ status' = "none"
    /\ renderAttempted' = (~GuardView \/ view = 0)
    /\ renderView' = view
    /\ draftCleared' = ((~GuardView \/ view = 0) /\ (~GuardDraft \/ draftOwner = submittedDraft))
    /\ clearedOwner' = draftOwner
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, view, draftOwner,
                   submittedDraft, lastReadId, reads, parking>>

Refresh ==
    /\ phase \in {"confirmed", "refresh_required"}
    /\ phase' = "refreshing"
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, status,
                   view, draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

RefreshFailure ==
    /\ phase = "refreshing"
    /\ phase' = IF PreserveReceipt THEN "refresh_required" ELSE "unconfirmed"
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, status,
                   view, draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

Synced ==
    /\ phase = "refreshing" /\ phase' = "complete"
    /\ UNCHANGED <<implicit, started, requestId, posts, sent, known, status,
                   view, draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

NextTurn ==
    /\ phase = "complete" /\ ~parking.retained /\ phase' = "next" /\ posts' = posts + 1
    /\ UNCHANGED <<implicit, started, requestId, sent, known, status, view,
                   draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

Input ==
    /\ phase \in {"creating", "submitting", "unconfirmed", "checking"}
    /\ draftOwner = 0 /\ draftOwner' = 1
    /\ UNCHANGED <<phase, implicit, started, requestId, posts, sent, known, status,
                   view, submittedDraft, draftCleared, clearedOwner, renderView,
                   renderAttempted, lastReadId, reads, parking>>

Navigate ==
    /\ phase \in {"submitting", "unconfirmed", "checking"}
    /\ view = 0 /\ view' = 1
    /\ UNCHANGED <<phase, implicit, started, requestId, posts, sent, known, status,
                   draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads, parking>>

Park ==
    /\ phase = "unconfirmed" /\ ~parking.retained
    /\ status \in {"interrupted", "not_found", "reconciliation_required"}
    /\ parking' = [parking EXCEPT !.retained = TRUE]
    /\ view' = 1 /\ draftOwner' = 1
    /\ UNCHANGED <<phase, implicit, started, requestId, posts, sent, known, status,
                   submittedDraft, draftCleared, clearedOwner, renderView,
                   renderAttempted, lastReadId, reads>>

NextDistinct ==
    /\ parking.retained /\ ~parking.started
    /\ parking' = [parking EXCEPT !.started = TRUE, !.request = 2,
                     !.session = IF IsolateParked THEN 2 ELSE 1]
    /\ posts' = posts + 1
    /\ UNCHANGED <<phase, implicit, started, requestId, sent, known, status,
                   view, draftOwner, submittedDraft, draftCleared, clearedOwner,
                   renderView, renderAttempted, lastReadId, reads>>

Next == Start \/ CreateSuccess \/ CreateFailure \/ Post \/ LoseResponse \/ Check \/
        (\E value \in Statuses \ {"none"} : Observe(value)) \/ Receive \/
        Refresh \/ RefreshFailure \/ Synced \/ NextTurn \/ Input \/ Navigate \/ Park \/ NextDistinct
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in Phases /\ implicit \in BOOLEAN /\ started \in BOOLEAN
    /\ requestId \in 0..1 /\ posts \in 0..2 /\ sent \in BOOLEAN /\ known \in BOOLEAN
    /\ status \in Statuses /\ view \in 0..1 /\ draftOwner \in 0..1 /\ submittedDraft \in 0..1
    /\ draftCleared \in BOOLEAN /\ clearedOwner \in 0..1 /\ renderView \in 0..1
    /\ renderAttempted \in BOOLEAN /\ lastReadId \in 0..1 /\ reads \in 0..2
    /\ parking \in [retained: BOOLEAN, started: BOOLEAN, request: 0..2, session: 0..2]
RequestBeforePost == posts > 0 => requestId = 1
OriginalRequestReads == reads > 0 => lastReadId = 1
NoUnconfirmedNextSend == ~known => posts <= 1 \/ (parking.retained /\ parking.started /\ parking.session = 2)
NoParkedSameSessionSend == parking.started => parking.session = 2 /\ parking.request = 2
RetainedRequestIdentity == parking.retained => requestId = 1
KnownResultNeverUnknown == known => phase \notin {"unconfirmed", "checking"}
NoStaleDraftClear == draftCleared => clearedOwner = submittedDraft
NoCreationFailureClear == phase = "idle" => ~draftCleared
NoStaleRender == renderAttempted => renderView = 0
=============================================================================
