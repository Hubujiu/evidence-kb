--------------------------- MODULE SchemaLifecycle ---------------------------
EXTENDS Naturals
CONSTANTS Structured, InheritSeal, LocalSeal, CheckOwner, CheckPhysical, PreserveIdentity, CheckConsumer
VARIABLES handles, sealed, prepared, physical, closed, badPrepare, badClone,
          siblingPrepared, transactionSeen, failureSeen, recovered, ownerMismatch,
          identities, badIdentityClone, badConsumerClone, badSiblingSeal, cloneSeen, consumerRejected
Handles == {1, 2, 3}
Schemas == {"A", "B"}
None == "none"
Identity == <<"space", "dataset", "registration">>
OtherIdentity == <<"other-space", "other-dataset", "other-registration">>
vars == <<handles, sealed, prepared, physical, closed, badPrepare, badClone,
          siblingPrepared, transactionSeen, failureSeen, recovered, ownerMismatch,
          identities, badIdentityClone, badConsumerClone, badSiblingSeal, cloneSeen, consumerRejected>>
Init == /\ handles = {1} /\ sealed = [h \in Handles |-> FALSE]
        /\ prepared = [h \in Handles |-> None] /\ physical = None /\ closed = FALSE
        /\ badPrepare = FALSE /\ badClone = FALSE /\ siblingPrepared = FALSE
        /\ transactionSeen = FALSE /\ failureSeen = FALSE /\ recovered = FALSE /\ ownerMismatch = FALSE
        /\ identities = [h \in Handles |-> Identity] /\ badIdentityClone = FALSE
        /\ badConsumerClone = FALSE /\ badSiblingSeal = FALSE
        /\ cloneSeen = FALSE /\ consumerRejected = FALSE
Bind(h) == /\ h \notin handles /\ ~closed /\ handles' = handles \cup {h}
           /\ UNCHANGED <<sealed, prepared, physical, closed, badPrepare, badClone,
               siblingPrepared, transactionSeen, failureSeen, recovered, ownerMismatch,
                   identities, badIdentityClone, badConsumerClone, cloneSeen, consumerRejected, badSiblingSeal>>
Clone(source, target, matchingConsumer) ==
    /\ ~Structured /\ source \in handles /\ target \notin handles
    /\ LET allowed == matchingConsumer \/ ~CheckConsumer
       IN /\ handles' = IF allowed THEN handles \cup {target} ELSE handles
          /\ sealed' = IF allowed THEN [sealed EXCEPT ![target] = IF InheritSeal THEN sealed[source] ELSE FALSE]
                        ELSE sealed
          /\ identities' = IF allowed THEN [identities EXCEPT ![target] =
                                 IF PreserveIdentity THEN identities[source] ELSE OtherIdentity] ELSE identities
          /\ badClone' = (badClone \/ (allowed /\ sealed'[target] # sealed[source]))
          /\ badIdentityClone' = (badIdentityClone \/ (allowed /\ identities'[target] # identities[source]))
          /\ badConsumerClone' = (badConsumerClone \/ (allowed /\ ~matchingConsumer))
          /\ cloneSeen' = (cloneSeen \/ allowed)
          /\ consumerRejected' = (consumerRejected \/ ~allowed)
    /\ UNCHANGED <<prepared, physical, closed, badPrepare, siblingPrepared,
                   transactionSeen, failureSeen, recovered, ownerMismatch, badSiblingSeal>>
Prepare(h, schema, correctOwner, outcome) ==
    /\ h \in handles /\ ~sealed[h] /\ ~closed
    /\ LET allowed == (correctOwner \/ ~CheckOwner) /\ (physical \in {None, schema} \/ ~CheckPhysical)
           success == allowed /\ outcome = "success"
       IN /\ prepared' = IF success THEN [prepared EXCEPT ![h] = schema] ELSE prepared
          /\ physical' = IF allowed /\ outcome \in {"success", "durableFailure"} THEN schema ELSE physical
          /\ badPrepare' = (badPrepare \/ (success /\ (sealed[h] \/ (physical # None /\ physical # schema))))
          /\ ownerMismatch' = (ownerMismatch \/ (success /\ ~correctOwner))
          /\ siblingPrepared' = (siblingPrepared \/ (success /\ \E other \in handles \ {h}: sealed[other]))
          /\ failureSeen' = (failureSeen \/ (allowed /\ outcome # "success"))
          /\ recovered' = (recovered \/ (success /\ failureSeen))
    /\ UNCHANGED <<handles, sealed, closed, badClone, transactionSeen,
                   identities, badIdentityClone, badConsumerClone, cloneSeen, consumerRejected, badSiblingSeal>>
Seal(h) == /\ h \in handles /\ (~Structured \/ prepared[h] # None)
           /\ sealed' = IF LocalSeal THEN [sealed EXCEPT ![h] = TRUE] ELSE [other \in Handles |-> IF other \in handles THEN TRUE ELSE sealed[other]]
           /\ badSiblingSeal' = (badSiblingSeal \/ (\E other \in handles \ {h}: sealed'[other] # sealed[other]))
           /\ UNCHANGED <<handles, prepared, physical, closed, badPrepare, badClone,
               siblingPrepared, transactionSeen, failureSeen, recovered, ownerMismatch,
                   identities, badIdentityClone, badConsumerClone, cloneSeen, consumerRejected>>
Transaction(h, validMode) ==
    /\ h \in handles /\ validMode /\ (~Structured \/ prepared[h] # None)
    /\ sealed' = [sealed EXCEPT ![h] = TRUE]
    /\ transactionSeen' = (transactionSeen \/ ~closed)
    /\ UNCHANGED <<handles, prepared, physical, closed, badPrepare, badClone,
                   siblingPrepared, failureSeen, recovered, ownerMismatch,
                   identities, badIdentityClone, badConsumerClone, cloneSeen, consumerRejected, badSiblingSeal>>
Close == /\ ~closed /\ closed' = TRUE
         /\ UNCHANGED <<handles, sealed, prepared, physical, badPrepare, badClone,
             siblingPrepared, transactionSeen, failureSeen, recovered, ownerMismatch,
                   identities, badIdentityClone, badConsumerClone, cloneSeen, consumerRejected, badSiblingSeal>>
Next == (\E h \in Handles: Bind(h) \/ Seal(h) \/ (\E valid \in BOOLEAN: Transaction(h, valid)))
        \/ (\E source, target \in Handles, matching \in BOOLEAN: Clone(source, target, matching))
        \/ (\E h \in Handles, schema \in Schemas, owner \in BOOLEAN,
                  outcome \in {"success", "failure", "durableFailure"}: Prepare(h, schema, owner, outcome))
        \/ Close
Spec == Init /\ [][Next]_vars
TypeOK == /\ identities \in [Handles -> {Identity, OtherIdentity}]
          /\ <<badIdentityClone, badConsumerClone, badSiblingSeal, cloneSeen, consumerRejected>> \in BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN
          /\ handles \subseteq Handles /\ sealed \in [Handles -> BOOLEAN]
          /\ prepared \in [Handles -> Schemas \cup {None}] /\ physical \in Schemas \cup {None}
          /\ <<closed, badPrepare, badClone, siblingPrepared, transactionSeen,
                failureSeen, recovered, ownerMismatch>> \in BOOLEAN \X BOOLEAN \X BOOLEAN
                \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN \X BOOLEAN
ClonePreservesIdentity == ~badIdentityClone
ConsumerNameChecked == ~badConsumerClone
SiblingSealUnchanged == ~badSiblingSeal
NoCloneWitness == ~cloneSeen
NoConsumerRejectionWitness == ~consumerRejected
NoSchemaReplacement == ~badPrepare
ConsumerInheritsSeal == ~badClone
CorrectOwner == ~ownerMismatch
UnboundHandlesStayOpen == \A h \in Handles \ handles: ~sealed[h]
PreparedMatchesPhysical == \A h \in handles: prepared[h] # None => prepared[h] = physical
NoSiblingPrepareWitness == ~siblingPrepared
NoTransactionWitness == ~transactionSeen
NoRecoveryWitness == ~recovered
=============================================================================
