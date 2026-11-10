--------------------------- MODULE SearchGeneration --------------------------
EXTENDS Naturals, TLC
CONSTANTS SkipSeals, SkipCanonical, SkipCAS
Generations == {1, 2}
Channels == {"source", "bm25", "dense"}
VARIABLES content, seals, frozenContent, active, pointerRevision, pending, expectedRevision,
          publicationSafe, canonicalSafe, casSafe
vars == <<content, seals, frozenContent, active, pointerRevision, pending, expectedRevision,
          publicationSafe, canonicalSafe, casSafe>>

Init ==
    /\ content = [g \in Generations |-> [c \in Channels |-> 0]]
    /\ seals = [g \in Generations |-> [c \in Channels |-> FALSE]]
    /\ frozenContent = [g \in Generations |-> [c \in Channels |-> 0]]
    /\ active = 0 /\ pointerRevision = 0
    /\ pending = [g \in Generations |-> "idle"]
    /\ expectedRevision = [g \in Generations |-> 0]
    /\ publicationSafe = TRUE /\ canonicalSafe = TRUE /\ casSafe = TRUE

Canonical(g) == content[g]["source"] # 0 /\
                 \A c \in Channels : content[g][c] = content[g]["source"]
Sealed(g) == \A c \in Channels : seals[g][c]

Stage(g, c, value) ==
    /\ content[g][c] = 0 /\ (c = "source" \/ content[g]["source"] # 0)
    \* A mismatched backend record is permitted as a fault; readback must refuse it.
    /\ content' = [content EXCEPT ![g][c] = value]
    /\ UNCHANGED <<seals, frozenContent, active, pointerRevision, pending, expectedRevision,
                   publicationSafe, canonicalSafe, casSafe>>

Seal(g, c) ==
    /\ ~seals[g][c] /\ (Canonical(g) \/ SkipCanonical)
    /\ content[g][c] # 0
    \* One exact token binds generation/content/dimension/dataset/checksum.
    /\ seals' = [seals EXCEPT ![g][c] = TRUE]
    /\ frozenContent' = [frozenContent EXCEPT ![g][c] = content[g][c]]
    /\ UNCHANGED <<content, active, pointerRevision, pending, expectedRevision,
                   publicationSafe, canonicalSafe, casSafe>>

BeginPublish(g) ==
    /\ pending[g] = "idle"
    /\ (Sealed(g) \/ SkipSeals) /\ (Canonical(g) \/ SkipCanonical)
    /\ pending' = [pending EXCEPT ![g] = "pending"]
    /\ expectedRevision' = [expectedRevision EXCEPT ![g] = pointerRevision]
    /\ UNCHANGED <<content, seals, frozenContent, active, pointerRevision,
                   publicationSafe, canonicalSafe, casSafe>>

CommitPublish(g) ==
    /\ pending[g] = "pending" /\ pointerRevision < 2
    /\ SkipCAS \/ expectedRevision[g] = pointerRevision
    /\ active' = g /\ pointerRevision' = pointerRevision + 1
    /\ pending' = [pending EXCEPT ![g] = "done"]
    /\ publicationSafe' = (publicationSafe /\ Sealed(g))
    /\ canonicalSafe' = (canonicalSafe /\ Canonical(g))
    /\ casSafe' = (casSafe /\ expectedRevision[g] = pointerRevision)
    /\ UNCHANGED <<content, seals, frozenContent, expectedRevision>>

RejectCAS(g) ==
    /\ pending[g] = "pending" /\ expectedRevision[g] # pointerRevision
    /\ pending' = [pending EXCEPT ![g] = "rejected"]
    /\ UNCHANGED <<content, seals, frozenContent, active, pointerRevision, expectedRevision,
                   publicationSafe, canonicalSafe, casSafe>>

Restart(g) ==
    /\ pending[g] = "pending"
    /\ pending' = [pending EXCEPT ![g] = "rejected"]
    \* Lose the request-local CAS snapshot, not any committed generation/pointer.
    /\ UNCHANGED <<content, seals, frozenContent, active, pointerRevision, expectedRevision,
                   publicationSafe, canonicalSafe, casSafe>>

Next == (\E g \in Generations, c \in Channels, value \in {1, 2} : Stage(g, c, value))
        \/ (\E g \in Generations, c \in Channels : Seal(g, c))
        \/ (\E g \in Generations : BeginPublish(g) \/ CommitPublish(g) \/ RejectCAS(g) \/ Restart(g))
\* Failed per-channel writes/commit rollback stutter on durable state.
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ content \in [Generations -> [Channels -> {0, 1, 2}]]
    /\ seals \in [Generations -> [Channels -> BOOLEAN]]
    /\ frozenContent \in [Generations -> [Channels -> {0, 1, 2}]]
    /\ active \in {0, 1, 2} /\ pointerRevision \in 0..2
    /\ pending \in [Generations -> {"idle", "pending", "done", "rejected"}]
    /\ expectedRevision \in [Generations -> 0..2]
    /\ publicationSafe \in BOOLEAN /\ canonicalSafe \in BOOLEAN /\ casSafe \in BOOLEAN
NoPartialPublication == publicationSafe
CanonicalPublication == canonicalSafe
CompareAndSwapPublication == casSafe
SealedContentImmutable == \A g \in Generations, c \in Channels :
    seals[g][c] => (content[g][c] # 0 /\ content[g][c] = frozenContent[g][c])
=============================================================================
