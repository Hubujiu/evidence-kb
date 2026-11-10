-------------------------- MODULE PlatformSnapshots --------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS CheckBinding, CheckAuthorization, RecheckSnapshot, CloseStreams, CheckCapacity, CheckOffset
Handles == {"a", "b"}
VARIABLES slot, position, streamOpen, expired, reading, bound, allowed,
          revoked, closed, delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset
vars == <<slot, position, streamOpen, expired, reading, bound, allowed,
          revoked, closed, delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Init == /\ slot = [h \in Handles |-> "new"] /\ position = [h \in Handles |-> 0]
        /\ streamOpen = {} /\ expired = {} /\ reading = [h \in Handles |-> "idle"]
        /\ bound = [h \in Handles |-> TRUE] /\ allowed = [h \in Handles |-> FALSE]
        /\ revoked = FALSE /\ closed = FALSE /\ delivered = {}
        /\ wrongBinding = {} /\ unauthorized = {} /\ staleAccess = {}
        /\ building = {} /\ built = {} /\ failedBuild = {}
        /\ requestedOffset = [h \in Handles |-> 0] /\ wrongOffset = {}
Reserve(h) == /\ ~closed /\ slot[h] = "new"
              /\ (CheckCapacity => Cardinality({i \in Handles : slot[i] \in {"reserved", "published"}}) < 1)
              /\ slot' = [slot EXCEPT ![h] = "reserved"]
              /\ UNCHANGED <<position, streamOpen, expired, reading, bound, allowed, revoked, closed,
                             delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Build(h) == /\ slot[h] = "reserved" /\ h \notin built
            /\ building' = building \cup {h} /\ built' = built \cup {h}
            /\ UNCHANGED <<slot, position, streamOpen, expired, reading, bound, allowed, revoked, closed,
                           delivered, wrongBinding, unauthorized, staleAccess, failedBuild, requestedOffset, wrongOffset>>
BuildFail(h) == /\ h \in building
                /\ building' = IF CloseStreams THEN building \ {h} ELSE building
                /\ failedBuild' = failedBuild \cup {h}
                /\ slot' = [slot EXCEPT ![h] = "disposed"]
                /\ UNCHANGED <<position, streamOpen, expired, reading, bound, allowed, revoked, closed,
                               delivered, wrongBinding, unauthorized, staleAccess, built, requestedOffset, wrongOffset>>
Publish(h) == /\ ~closed /\ slot[h] = "reserved" /\ h \in building /\ h \notin failedBuild
              /\ slot' = [slot EXCEPT ![h] = "published"] /\ streamOpen' = streamOpen \cup {h}
              /\ building' = building \ {h}
              /\ UNCHANGED <<position, expired, reading, bound, allowed, revoked, closed,
                             delivered, wrongBinding, unauthorized, staleAccess, built, failedBuild, requestedOffset, wrongOffset>>
Discard(h) == /\ slot[h] \in {"reserved", "published"}
              /\ slot' = [slot EXCEPT ![h] = "disposed"]
              /\ streamOpen' = IF CloseStreams THEN streamOpen \ {h} ELSE streamOpen
              /\ UNCHANGED <<position, expired, reading, bound, allowed, revoked, closed,
                             delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Age(h) == /\ slot[h] = "published" /\ h \notin expired
          /\ expired' = expired \cup {h}
          /\ UNCHANGED <<slot, position, streamOpen, reading, bound, allowed, revoked, closed,
                         delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Capture(h, matching, offset) == /\ slot[h] = "published" /\ h \notin expired /\ reading[h] = "idle"
                       /\ (CheckBinding => matching)
                       /\ reading' = [reading EXCEPT ![h] = "captured"]
                       /\ bound' = [bound EXCEPT ![h] = matching]
                       /\ requestedOffset' = [requestedOffset EXCEPT ![h] = offset]
                       /\ UNCHANGED <<slot, position, streamOpen, expired, allowed, revoked, closed,
                                      delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, wrongOffset>>
Authorize(h) == /\ reading[h] = "captured"
                /\ reading' = [reading EXCEPT ![h] = "authorized"]
                /\ allowed' = [allowed EXCEPT ![h] = ~revoked]
                /\ UNCHANGED <<slot, position, streamOpen, expired, bound, revoked, closed,
                               delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Read(h) == /\ reading[h] = "authorized" /\ (CheckAuthorization => allowed[h])
           /\ (RecheckSnapshot => slot[h] = "published" /\ h \notin expired)
           /\ (CheckOffset => requestedOffset[h] = position[h])
           /\ reading' = [reading EXCEPT ![h] = "idle"]
           /\ staleAccess' = IF slot[h] = "published" /\ h \notin expired THEN staleAccess ELSE staleAccess \cup {h}
           /\ wrongBinding' = IF bound[h] THEN wrongBinding ELSE wrongBinding \cup {h}
           /\ unauthorized' = IF allowed[h] THEN unauthorized ELSE unauthorized \cup {h}
           /\ delivered' = delivered \cup {h}
           /\ wrongOffset' = IF requestedOffset[h] = position[h] THEN wrongOffset ELSE wrongOffset \cup {h}
           /\ position' = [position EXCEPT ![h] = IF @ < 2 THEN @ + 1 ELSE @]
           /\ slot' = IF position[h] = 1 THEN [slot EXCEPT ![h] = "disposed"] ELSE slot
           /\ streamOpen' = IF position[h] = 1 /\ CloseStreams THEN streamOpen \ {h} ELSE streamOpen
           /\ UNCHANGED <<expired, bound, allowed, revoked, closed, building, built, failedBuild, requestedOffset>>
Reject(h) == /\ reading[h] = "authorized"
             /\ (~allowed[h] \/ slot[h] # "published" \/ h \in expired)
             /\ reading' = [reading EXCEPT ![h] = "rejected"]
             /\ slot' = [slot EXCEPT ![h] = "disposed"]
             /\ streamOpen' = IF CloseStreams THEN streamOpen \ {h} ELSE streamOpen
             /\ UNCHANGED <<position, expired, bound, allowed, revoked, closed,
                            delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
OffsetReject(h) == /\ reading[h] = "authorized" /\ allowed[h]
                   /\ slot[h] = "published" /\ h \notin expired /\ requestedOffset[h] # position[h]
                   /\ reading' = [reading EXCEPT ![h] = "idle"]
                   /\ UNCHANGED <<slot, position, streamOpen, expired, bound, allowed, revoked, closed,
                                  delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Revoke == /\ ~revoked /\ revoked' = TRUE
          /\ UNCHANGED <<slot, position, streamOpen, expired, reading, bound, allowed, closed,
                         delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Close == /\ ~closed /\ closed' = TRUE
         /\ slot' = [h \in Handles |-> IF slot[h] \in {"reserved", "published"} THEN "disposed" ELSE slot[h]]
         /\ streamOpen' = IF CloseStreams THEN {} ELSE streamOpen
         /\ UNCHANGED <<position, expired, reading, bound, allowed, revoked,
                        delivered, wrongBinding, unauthorized, staleAccess, building, built, failedBuild, requestedOffset, wrongOffset>>
Next == Revoke \/ Close \/ \E h \in Handles : Reserve(h) \/ Build(h) \/ BuildFail(h) \/ Publish(h) \/ Discard(h) \/ Age(h)
             \/ (\E matching \in BOOLEAN, offset \in 0..2 : Capture(h, matching, offset)) \/ Authorize(h) \/ Read(h) \/ Reject(h) \/ OffsetReject(h)
Spec == Init /\ [][Next]_vars
TypeOK == /\ slot \in [Handles -> {"new", "reserved", "published", "disposed"}]
          /\ position \in [Handles -> 0..2] /\ streamOpen \subseteq Handles /\ expired \subseteq Handles
          /\ reading \in [Handles -> {"idle", "captured", "authorized", "rejected"}]
          /\ bound \in [Handles -> BOOLEAN] /\ allowed \in [Handles -> BOOLEAN]
          /\ revoked \in BOOLEAN /\ closed \in BOOLEAN /\ delivered \subseteq Handles
          /\ wrongBinding \subseteq Handles /\ unauthorized \subseteq Handles /\ staleAccess \subseteq Handles
          /\ building \subseteq Handles /\ built \subseteq Handles /\ failedBuild \subseteq Handles
          /\ requestedOffset \in [Handles -> 0..2] /\ wrongOffset \subseteq Handles
CapacityBound == Cardinality({h \in Handles : slot[h] \in {"reserved", "published"}}) <= 1
FailedBuildReleasesStream == building \intersect failedBuild = {}
ExactOffset == wrongOffset = {}
ActorAndKindBound == wrongBinding = {}
AuthorizationAtSample == unauthorized = {}
NoStaleStreamAccess == staleAccess = {}
OwnedStreamsOnly == \A h \in streamOpen : slot[h] = "published"
ClosedOwnsNothing == closed => streamOpen = {} /\ \A h \in Handles : slot[h] \notin {"reserved", "published"}
NoCompletedWitness == ~(\E h \in Handles : position[h] = 2 /\ slot[h] = "disposed" /\ h \in delivered)
NoRevokedWitness == ~(revoked /\ \E h \in Handles : reading[h] = "rejected" /\ ~allowed[h])
NoClosedReadWitness == ~(closed /\ \E h \in Handles : reading[h] = "rejected" /\ allowed[h])
NoClosedBuildWitness == ~(closed /\ \E h \in Handles : h \in failedBuild /\ h \notin building /\ h \in built)
=============================================================================
