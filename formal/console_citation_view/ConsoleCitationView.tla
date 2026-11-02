------------------------ MODULE ConsoleCitationView ------------------------
EXTENDS Naturals, TLC

CONSTANTS GuardSelection, GuardPreviewBody, GuardFailure
Requests == {"A", "B"}
NoOwner == "none"
Owners == Requests \cup {NoOwner}
Phases == {"idle", "metadata-fetch", "metadata-body", "preview-fetch",
           "preview-body", "error-ready", "complete", "discarded", "failed"}
Pending == {"metadata-fetch", "metadata-body", "preview-fetch", "preview-body"}

VARIABLES scope, selected, phase, requestScope, metadata, preview, download,
          currentURL, urlState, message, lastRender, lastRelease, lastFailure
vars == <<scope, selected, phase, requestScope, metadata, preview, download,
          currentURL, urlState, message, lastRender, lastRelease, lastFailure>>

EmptyWrite == [actor |-> NoOwner, atScope |-> 0, atSelection |-> NoOwner]
WriteBy(r) == [actor |-> r, atScope |-> scope, atSelection |-> selected]
ScopeMatches(r) == requestScope[r] = scope
Current(r) == ScopeMatches(r) /\ selected = r
MetadataAllowed(r) == ScopeMatches(r) /\ (~GuardSelection \/ selected = r)
PreviewAllowed(r) == ~GuardPreviewBody \/ MetadataAllowed(r)
ReleaseCurrent == IF currentURL = NoOwner THEN urlState
                  ELSE [urlState EXCEPT ![currentURL] = "revoked"]

Init ==
    /\ scope = 0
    /\ selected = NoOwner
    /\ phase = [r \in Requests |-> "idle"]
    /\ requestScope = [r \in Requests |-> 0]
    /\ metadata = NoOwner /\ preview = NoOwner /\ download = NoOwner
    /\ currentURL = NoOwner
    /\ urlState = [r \in Requests |-> "unallocated"]
    /\ message = NoOwner
    /\ lastRender = EmptyWrite /\ lastRelease = EmptyWrite
    /\ lastFailure = EmptyWrite

Select(r) ==
    /\ phase[r] = "idle"
    /\ selected' = r
    /\ phase' = [phase EXCEPT ![r] = "metadata-fetch"]
    /\ requestScope' = [requestScope EXCEPT ![r] = scope]
    /\ UNCHANGED <<scope, metadata, preview, download, currentURL, urlState,
                   message, lastRender, lastRelease, lastFailure>>

SwitchScope ==
    /\ scope = 0
    /\ scope' = 1
    /\ selected' = NoOwner
    /\ metadata' = NoOwner /\ preview' = NoOwner /\ download' = NoOwner
    /\ urlState' = ReleaseCurrent
    /\ currentURL' = NoOwner
    /\ message' = NoOwner
    /\ lastRender' = EmptyWrite /\ lastRelease' = EmptyWrite
    /\ lastFailure' = EmptyWrite
    /\ UNCHANGED <<phase, requestScope>>

MetadataHeaders(r) ==
    /\ phase[r] = "metadata-fetch"
    /\ phase' = [phase EXCEPT ![r] =
          IF ScopeMatches(r) THEN "metadata-body" ELSE "discarded"]
    /\ UNCHANGED <<scope, selected, requestScope, metadata, preview, download,
                   currentURL, urlState, message, lastRender, lastRelease,
                   lastFailure>>

MetadataBody(r) ==
    /\ phase[r] = "metadata-body"
    /\ phase' = [phase EXCEPT ![r] =
          IF MetadataAllowed(r) THEN "preview-fetch" ELSE "discarded"]
    /\ metadata' = IF MetadataAllowed(r) THEN r ELSE metadata
    /\ preview' = IF MetadataAllowed(r) THEN NoOwner ELSE preview
    /\ download' = IF MetadataAllowed(r) THEN NoOwner ELSE download
    /\ lastRender' = IF MetadataAllowed(r) THEN WriteBy(r) ELSE lastRender
    /\ UNCHANGED <<scope, selected, requestScope, currentURL, urlState,
                   message, lastRelease, lastFailure>>

PreviewHeaders(r) ==
    /\ phase[r] = "preview-fetch"
    /\ phase' = [phase EXCEPT ![r] =
          IF ScopeMatches(r) THEN "preview-body" ELSE "discarded"]
    /\ UNCHANGED <<scope, selected, requestScope, metadata, preview, download,
                   currentURL, urlState, message, lastRender, lastRelease,
                   lastFailure>>

PreviewBody(r) ==
    /\ phase[r] = "preview-body"
    /\ phase' = [phase EXCEPT ![r] =
          IF PreviewAllowed(r) THEN "complete" ELSE "discarded"]
    /\ preview' = IF PreviewAllowed(r) THEN r ELSE preview
    /\ download' = IF PreviewAllowed(r) THEN r ELSE download
    /\ currentURL' = IF PreviewAllowed(r) THEN r ELSE currentURL
    /\ urlState' = IF PreviewAllowed(r)
          THEN [ReleaseCurrent EXCEPT ![r] = "live"] ELSE urlState
    /\ lastRender' = IF PreviewAllowed(r) THEN WriteBy(r) ELSE lastRender
    /\ lastRelease' = IF PreviewAllowed(r) /\ currentURL # NoOwner
          THEN WriteBy(r) ELSE lastRelease
    /\ UNCHANGED <<scope, selected, requestScope, metadata, message, lastFailure>>

Reject(r) ==
    /\ phase[r] \in Pending
    /\ phase' = [phase EXCEPT ![r] = "error-ready"]
    /\ UNCHANGED <<scope, selected, requestScope, metadata, preview, download,
                   currentURL, urlState, message, lastRender, lastRelease,
                   lastFailure>>

Failure(r) ==
    /\ phase[r] = "error-ready"
    /\ LET accepted == ~GuardFailure \/ Current(r)
       IN /\ phase' = [phase EXCEPT ![r] = IF accepted THEN "failed" ELSE "discarded"]
          /\ message' = IF accepted THEN r ELSE message
          /\ lastFailure' = IF accepted THEN WriteBy(r) ELSE lastFailure
    /\ UNCHANGED <<scope, selected, requestScope, metadata, preview, download,
                   currentURL, urlState, lastRender, lastRelease>>

Next == SwitchScope \/ \E r \in Requests :
    Select(r) \/ MetadataHeaders(r) \/ MetadataBody(r) \/
    PreviewHeaders(r) \/ PreviewBody(r) \/ Reject(r) \/ Failure(r)
Spec == Init /\ [][Next]_vars

WriteType == [actor : Owners, atScope : 0..1, atSelection : Owners]
TypeOK ==
    /\ scope \in 0..1 /\ selected \in Owners
    /\ phase \in [Requests -> Phases]
    /\ requestScope \in [Requests -> 0..1]
    /\ metadata \in Owners /\ preview \in Owners /\ download \in Owners
    /\ currentURL \in Owners /\ message \in Owners
    /\ urlState \in [Requests -> {"unallocated", "live", "revoked"}]
    /\ lastRender \in WriteType /\ lastRelease \in WriteType
    /\ lastFailure \in WriteType

CurrentWrite(write) == write.actor = NoOwner \/
    (requestScope[write.actor] = write.atScope /\ write.actor = write.atSelection)
NoStaleRender == CurrentWrite(lastRender)
NoStaleURLRelease == CurrentWrite(lastRelease)
NoStaleFailure == CurrentWrite(lastFailure)
CitationIdentityCoherent ==
    /\ preview # NoOwner => preview = metadata
    /\ download # NoOwner => download = metadata
    /\ preview # NoOwner => currentURL = preview
CurrentURLLive == currentURL # NoOwner => urlState[currentURL] = "live"
AtMostOneLiveURL == \A left, right \in Requests :
    (urlState[left] = "live" /\ urlState[right] = "live") => left = right
=============================================================================
