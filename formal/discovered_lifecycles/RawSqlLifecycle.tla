-------------------------- MODULE RawSqlLifecycle --------------------------
EXTENDS Naturals
CONSTANTS AllowSealedPrepare, SkipImplicitSeal, SealInvalidMode
VARIABLE s
vars == <<s>>
Init == s = [sealed |-> FALSE, prepares |-> 0, transactions |-> 0,
             enterFailed |-> FALSE, invalidSeen |-> FALSE, badPrepare |-> FALSE,
             badInvalid |-> FALSE, entered |-> FALSE]
Prepare == /\ s.prepares < 2 /\ (~s.sealed \/ AllowSealedPrepare)
           /\ s' = [s EXCEPT !.prepares = @ + 1, !.badPrepare = (s.badPrepare \/ s.sealed)]
Seal == /\ ~s.sealed /\ s' = [s EXCEPT !.sealed = TRUE]
Transaction(success) ==
    /\ s.transactions < 2
    /\ s' = [s EXCEPT !.sealed = (s.sealed \/ ~SkipImplicitSeal),
                       !.transactions = @ + 1, !.entered = (s.entered \/ success),
                       !.enterFailed = (s.enterFailed \/ ~success)]
InvalidMode == /\ ~s.invalidSeen
               /\ s' = [s EXCEPT !.invalidSeen = TRUE,
                          !.sealed = (s.sealed \/ SealInvalidMode),
                          !.badInvalid = (~s.sealed /\ SealInvalidMode)]
Next == Prepare \/ Seal \/ InvalidMode \/ (\E success \in BOOLEAN: Transaction(success))
Spec == Init /\ [][Next]_vars
TypeOK == /\ s.sealed \in BOOLEAN /\ s.prepares \in 0..2 /\ s.transactions \in 0..2
NoPrepareAfterSeal == ~s.badPrepare
TransactionSeals == s.transactions > 0 => s.sealed
InvalidModePreservesAdmission == ~s.badInvalid
WitnessUnpreparedTransaction == ~(s.entered /\ s.prepares = 0 /\ s.sealed)
WitnessFailedEnterSeals == ~(s.enterFailed /\ s.sealed)
=============================================================================
