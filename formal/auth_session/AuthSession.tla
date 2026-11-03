-------------------------- MODULE AuthSession --------------------------
EXTENDS Naturals
CONSTANT SkipExpiry
Tokens == 1..2
Actors == {<<1, 1>>, <<1, 2>>, <<2, 1>>, <<2, 2>>}
VARIABLES records, owners, issued, now, phase, requested, snapshot, snapshotOwner,
          authenticated, returnedOwner, checkedAt
vars == <<records, owners, issued, now, phase, requested, snapshot, snapshotOwner,
          authenticated, returnedOwner, checkedAt>>
Init == /\ records = [t \in Tokens |-> "absent"]
        /\ owners = [t \in Tokens |-> <<1, 1>>] /\ issued = {} /\ now = 0
        /\ phase = "new" /\ requested = 1 /\ snapshot = "absent" /\ snapshotOwner = <<1, 1>>
        /\ authenticated = FALSE /\ returnedOwner = <<1, 1>> /\ checkedAt = 0
Issue(t, a) == /\ now = 0 /\ t \notin issued
               /\ records' = [records EXCEPT ![t] = "valid"]
               /\ owners' = [owners EXCEPT ![t] = a] /\ issued' = issued \cup {t}
               /\ UNCHANGED <<now, phase, requested, snapshot, snapshotOwner,
                              authenticated, returnedOwner, checkedAt>>
Revoke(t) == /\ records[t] # "absent"
             /\ records' = [records EXCEPT ![t] = "absent"]
             /\ UNCHANGED <<owners, issued, now, phase, requested, snapshot, snapshotOwner,
                            authenticated, returnedOwner, checkedAt>>
Corrupt(t) == /\ records[t] = "valid"
              /\ records' = [records EXCEPT ![t] = "malformed"]
              /\ UNCHANGED <<owners, issued, now, phase, requested, snapshot, snapshotOwner,
                             authenticated, returnedOwner, checkedAt>>
Tick == /\ now < 3 /\ now' = now + 1
        /\ UNCHANGED <<records, owners, issued, phase, requested, snapshot, snapshotOwner,
                       authenticated, returnedOwner, checkedAt>>
Read(t, fails) == /\ phase = "new" /\ requested' = t /\ phase' = "loaded"
                  /\ snapshot' = IF fails THEN "backend-error" ELSE records[t]
                  /\ snapshotOwner' = owners[t]
                  /\ UNCHANGED <<records, owners, issued, now, authenticated, returnedOwner, checkedAt>>
Validate == /\ phase = "loaded" /\ phase' = "answered" /\ checkedAt' = now
            /\ authenticated' = (snapshot = "valid" /\ (SkipExpiry \/ now < 2))
            /\ returnedOwner' = snapshotOwner
            /\ UNCHANGED <<records, owners, issued, now, requested, snapshot, snapshotOwner>>
Next == (\E t \in Tokens : Revoke(t) \/ Corrupt(t)
           \/ (\E a \in Actors : Issue(t, a)) \/ (\E fails \in BOOLEAN : Read(t, fails))) \/ Tick \/ Validate
Spec == Init /\ [][Next]_vars
TypeOK == /\ records \in [Tokens -> {"absent", "valid", "malformed"}]
          /\ owners \in [Tokens -> Actors] /\ issued \subseteq Tokens /\ now \in 0..3
          /\ phase \in {"new", "loaded", "answered"} /\ requested \in Tokens
          /\ snapshot \in {"absent", "valid", "malformed", "backend-error"}
          /\ snapshotOwner \in Actors /\ returnedOwner \in Actors
          /\ authenticated \in BOOLEAN /\ checkedAt \in 0..3
ValidSnapshotOnly == authenticated => snapshot = "valid" /\ requested \in issued
ActorTenantIsolation == authenticated => returnedOwner = owners[requested]
OwnExpiryEnforced == authenticated => checkedAt < 2
=============================================================================
