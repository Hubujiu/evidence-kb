-------------------------- MODULE SchemaNegotiation --------------------------
EXTENDS Naturals
CONSTANTS FreezeIdentity, PublishAfterSuccess, ValidatePrepareAck, ValidateSealAck, RejectClosed, ForeignSchema
VARIABLES configured, physical, prepared, sealed, closed, pending, response,
          accepted, attempts, badAdmission, badRetry, badPublication, clientSuccess,
          observedAck, observedSuccess, retrySeen, lossSeen, failedSeen, beginSeen
Keys == {"A", "B", "foreignA", "versionA"}
None == "none"
Digest(key) == IF key = "B" THEN "digest-B" ELSE "digest-A"
Owner(key) == IF key = "foreignA" THEN "other" ELSE "owner"
Declared == {configured} \cup (IF ForeignSchema THEN {"foreignA"} ELSE {})
vars == <<configured, physical, prepared, sealed, closed, pending, response,
          accepted, attempts, badAdmission, badRetry, badPublication, clientSuccess,
          observedAck, observedSuccess, retrySeen, lossSeen, failedSeen, beginSeen>>
Init == /\ configured = "A" /\ physical = None /\ prepared = None
        /\ sealed = FALSE /\ closed = FALSE /\ pending = None /\ response = None
        /\ accepted = FALSE /\ attempts = 0 /\ badAdmission = FALSE
        /\ badRetry = FALSE /\ badPublication = FALSE /\ clientSuccess = FALSE
        /\ observedAck = None /\ observedSuccess = FALSE /\ retrySeen = FALSE
        /\ lossSeen = FALSE /\ failedSeen = FALSE /\ beginSeen = FALSE
Prepare(key, outcome) ==
    /\ pending = None /\ attempts < 3
    /\ LET cached == prepared # None
           same == IF FreezeIdentity THEN prepared = key ELSE Digest(configured) = Digest(key)
           admitted == (~closed \/ ~RejectClosed) /\ key \in Declared
           repeat == admitted /\ cached /\ same
           fresh == admitted /\ ~cached /\ ~sealed /\ Owner(key) = "owner"
                    /\ physical \in {None, key}
           success == repeat \/ (fresh /\ outcome = "success")
           publish == fresh /\ (outcome = "success" \/ ~PublishAfterSuccess)
       IN /\ prepared' = IF publish THEN key ELSE prepared
          /\ physical' = IF fresh /\ outcome \in {"success", "durableFailure"} THEN key ELSE physical
          /\ accepted' = success
          /\ response' = IF success THEN "PREPARED" ELSE "error"
          /\ badAdmission' = (badAdmission \/ (closed /\ success))
          /\ badRetry' = (badRetry \/ (repeat /\ key # prepared))
          /\ badPublication' = (badPublication \/ (publish /\ outcome # "success"))
          /\ retrySeen' = (retrySeen \/ (repeat /\ (sealed \/ lossSeen)))
          /\ failedSeen' = (failedSeen \/ (fresh /\ outcome # "success"))
    /\ pending' = "prepare" /\ attempts' = attempts + 1
    /\ UNCHANGED <<configured, sealed, closed, clientSuccess, observedAck,
                   observedSuccess, lossSeen, beginSeen>>
Seal == /\ pending = None
        /\ LET success == ~closed /\ prepared # None
           IN /\ sealed' = (sealed \/ success)
              /\ accepted' = success
              /\ response' = IF success THEN "SEALED" ELSE "error"
        /\ pending' = "seal"
        /\ UNCHANGED <<configured, physical, prepared, closed, attempts, badAdmission,
                       badRetry, badPublication, clientSuccess, observedAck,
                       observedSuccess, retrySeen, lossSeen, failedSeen, beginSeen>>
Observe(ack) ==
    /\ pending # None /\ ack \in {response, "invalid", "lost"}
    /\ clientSuccess' = IF (IF pending = "prepare" THEN ValidatePrepareAck ELSE ValidateSealAck)
                        THEN ack = (IF pending = "prepare" THEN "PREPARED" ELSE "SEALED")
                        ELSE ack # "error" /\ ack # "lost"
    /\ observedAck' = ack /\ observedSuccess' = accepted
    /\ lossSeen' = (lossSeen \/ ack = "lost")
    /\ pending' = None /\ response' = None /\ accepted' = FALSE
    /\ UNCHANGED <<configured, physical, prepared, sealed, closed, attempts, badAdmission,
                   badRetry, badPublication, retrySeen, failedSeen, beginSeen>>
ChangeConfig == /\ pending = None /\ configured = "A" /\ configured' = "B"
                /\ UNCHANGED <<physical, prepared, sealed, closed, pending, response,
                    accepted, attempts, badAdmission, badRetry, badPublication, clientSuccess,
                    observedAck, observedSuccess, retrySeen, lossSeen, failedSeen, beginSeen>>
Begin == /\ pending = None /\ ~closed /\ prepared = configured /\ prepared # None
         /\ sealed' = TRUE /\ beginSeen' = TRUE
         /\ UNCHANGED <<configured, physical, prepared, closed, pending, response,
             accepted, attempts, badAdmission, badRetry, badPublication, clientSuccess,
             observedAck, observedSuccess, retrySeen, lossSeen, failedSeen>>
Close == /\ ~closed /\ closed' = TRUE
         /\ UNCHANGED <<configured, physical, prepared, sealed, pending, response,
             accepted, attempts, badAdmission, badRetry, badPublication, clientSuccess,
             observedAck, observedSuccess, retrySeen, lossSeen, failedSeen, beginSeen>>
Next == (\E key \in Keys, outcome \in {"success", "failure", "durableFailure"}: Prepare(key, outcome))
        \/ Seal \/ (\E ack \in {"PREPARED", "SEALED", "error", "invalid", "lost"}: Observe(ack))
        \/ ChangeConfig \/ Begin \/ Close
Spec == Init /\ [][Next]_vars
TypeOK == /\ configured \in {"A", "B"} /\ physical \in Keys \cup {None}
          /\ prepared \in Keys \cup {None} /\ attempts \in 0..3
          /\ pending \in {None, "prepare", "seal"}
          /\ response \in {None, "PREPARED", "SEALED", "error"}
          /\ observedAck \in {None, "PREPARED", "SEALED", "error", "invalid", "lost"}
          /\ <<sealed, closed, accepted, badAdmission, badRetry, badPublication, clientSuccess,
                observedSuccess, retrySeen, lossSeen, failedSeen, beginSeen>> \in BOOLEAN \X BOOLEAN \X BOOLEAN
                \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN
PreparedIsDurable == prepared # None => prepared = physical /\ Owner(prepared) = "owner"
OnlySuccessfulPublication == ~badPublication
RetryMatchesPreparedIdentity == ~badRetry
NoClosedAdmission == ~badAdmission
ExactAcknowledgement == clientSuccess => observedSuccess /\ observedAck \in {"PREPARED", "SEALED"}
NoRetryWitness == ~retrySeen
NoFailureRecoveryWitness == ~(failedSeen /\ prepared # None)
NoLostAckRetryWitness == ~(lossSeen /\ retrySeen)
NoBeginWitness == ~beginSeen
=============================================================================
