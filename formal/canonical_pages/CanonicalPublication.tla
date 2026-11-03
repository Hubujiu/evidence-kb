----------------------- MODULE CanonicalPublication -----------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Bug, BuildRequired
VARIABLES stage, batch, sent, charged, validated, capsules, readback, receiptOK,
          authority, admitted, result, aggregate, built, everSent, sendCounts, failedAt
vars == <<stage, batch, sent, charged, validated, capsules, readback, receiptOK,
          authority, admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
Batches == 1..4
Sources == 1..2
Source(b) == IF b <= 2 THEN 1 ELSE 2
Required(s) == IF s = 1 THEN {1, 2} ELSE {3, 4}
Active == stage \notin {"DONE", "FAILED", "INTERRUPTED"}
Init == /\ stage = "FRESH" /\ batch = 1 /\ sent = {} /\ charged = {}
        /\ validated = {} /\ capsules = {} /\ readback = {} /\ receiptOK = FALSE
        /\ authority = TRUE /\ admitted = FALSE /\ result = "absent"
        /\ aggregate = FALSE /\ built = FALSE /\ everSent = {} /\ sendCounts = [b \in Batches |-> 0]
        /\ failedAt = "none"
Begin == /\ stage = "FRESH" /\ stage' = "PLAN"
         /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                         authority, admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
Plan == /\ stage = "PLAN" /\ stage' = "PRECHECK"
        /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                        authority, admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
Admit == /\ stage = "PRECHECK" /\ (authority \/ Bug = "skip-authority")
         /\ admitted' = authority /\ stage' = "SEND"
         /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                         authority, result, aggregate, built, everSent, sendCounts, failedAt>>
Send == /\ stage = "SEND"
        /\ sent' = sent \cup {batch} /\ charged' = charged \cup {batch}
        /\ everSent' = everSent \cup {batch} /\ stage' = "OBSERVE"
        /\ sendCounts' = [sendCounts EXCEPT ![batch] = @ + 1]
        /\ UNCHANGED <<batch, validated, capsules, readback, receiptOK, authority,
                        admitted, result, aggregate, built, failedAt>>
ObserveGood == /\ stage = "OBSERVE" /\ receiptOK' = TRUE /\ stage' = "RECEIPT"
               /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, authority,
                               admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
ObserveBad == /\ stage = "OBSERVE" /\ receiptOK' = FALSE /\ stage' = "RECEIPT"
              /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, authority,
                              admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
ObserveUnknown == /\ stage = "OBSERVE" /\ stage' = "FAILED" /\ result' = "error"
                  /\ failedAt' = "unknown"
                  /\ charged' = IF Bug = "unknown-zero" THEN charged \ {batch} ELSE charged
                  /\ UNCHANGED <<batch, sent, validated, capsules, readback, receiptOK, authority,
                                  admitted, aggregate, built, everSent, sendCounts>>
Validate == /\ stage = "RECEIPT" /\ (receiptOK \/ Bug = "skip-receipt")
            /\ validated' = validated \cup {batch} /\ stage' = "POSTCHECK"
            /\ UNCHANGED <<batch, sent, charged, capsules, readback, receiptOK, authority,
                            admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
Postcheck == /\ stage = "POSTCHECK" /\ authority
             /\ stage' = IF batch \in {2, 4} \/ Bug = "early-capsule" THEN "CAPSULE" ELSE "NEXT"
             /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                             authority, admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
WriteCapsule == /\ stage = "CAPSULE" /\ capsules' = capsules \cup {Source(batch)}
                /\ stage' = IF Bug = "skip-readback" THEN "NEXT" ELSE "READBACK"
                /\ UNCHANGED <<batch, sent, charged, validated, readback, receiptOK, authority,
                                admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
Readback == /\ stage = "READBACK" /\ authority /\ readback' = readback \cup {Source(batch)}
            /\ stage' = "NEXT"
            /\ UNCHANGED <<batch, sent, charged, validated, capsules, receiptOK, authority,
                            admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
NextBatch == /\ stage = "NEXT" /\ batch < 4 /\ batch' = batch + 1
             /\ receiptOK' = FALSE /\ admitted' = FALSE /\ stage' = "PRECHECK"
             /\ UNCHANGED <<sent, charged, validated, capsules, readback, authority,
                             result, aggregate, built, everSent, sendCounts, failedAt>>
Prepublish == /\ stage = "NEXT" /\ batch = 4 /\ authority /\ stage' = "AGGREGATE"
              /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                              authority, admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
PublishAggregate == /\ stage = "AGGREGATE" /\ aggregate' = TRUE
                    /\ stage' = IF BuildRequired THEN "BUILD" ELSE "RESULT"
                    /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                                    authority, admitted, result, built, everSent, sendCounts, failedAt>>
Build == /\ (stage = "BUILD" \/ (Bug = "early-build" /\ stage = "NEXT"))
         /\ built' = TRUE /\ stage' = "RESULT"
         /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                         authority, admitted, result, aggregate, everSent, sendCounts, failedAt>>
PublishResult == /\ stage = "RESULT" /\ result' = "complete" /\ stage' = "DONE"
                 /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                                 authority, admitted, aggregate, built, everSent, sendCounts, failedAt>>
Fail == /\ Active /\ stage \notin {"FRESH", "PLAN"}
        /\ stage' = "FAILED" /\ result' = "error" /\ failedAt' = stage
        /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                        authority, admitted, aggregate, built, everSent, sendCounts>>
Interrupt == /\ Active /\ stage # "FRESH" /\ stage' = "INTERRUPTED" /\ failedAt' = stage
             /\ UNCHANGED <<batch, sent, charged, validated, capsules, readback, receiptOK,
                             authority, admitted, result, aggregate, built, everSent, sendCounts>>
Revoke == /\ authority /\ authority' = FALSE
          /\ UNCHANGED <<stage, batch, sent, charged, validated, capsules, readback, receiptOK,
                          admitted, result, aggregate, built, everSent, sendCounts, failedAt>>
Reenter == /\ stage \in {"DONE", "FAILED", "INTERRUPTED"}
           /\ Bug = "reuse-output" /\ sent # {} /\ sendCounts[1] < 2
           /\ stage' = "PRECHECK" /\ batch' = 1 /\ admitted' = FALSE /\ receiptOK' = FALSE
           /\ UNCHANGED <<sent, charged, validated, capsules, readback, authority,
                           result, aggregate, built, everSent, sendCounts, failedAt>>
Next == Begin \/ Plan \/ Admit \/ Send \/ ObserveGood \/ ObserveBad \/ ObserveUnknown
        \/ Validate \/ Postcheck \/ WriteCapsule \/ Readback \/ NextBatch \/ Prepublish
        \/ PublishAggregate \/ Build \/ PublishResult \/ Fail \/ Interrupt \/ Revoke \/ Reenter
Spec == Init /\ [][Next]_vars
Stages == {"FRESH", "PLAN", "PRECHECK", "SEND", "OBSERVE", "RECEIPT", "POSTCHECK",
           "CAPSULE", "READBACK", "NEXT", "AGGREGATE", "BUILD", "RESULT", "DONE", "FAILED", "INTERRUPTED"}
TypeOK == /\ stage \in Stages /\ batch \in Batches
          /\ sent \subseteq Batches /\ charged \subseteq Batches /\ everSent \subseteq Batches
          /\ validated \subseteq Batches /\ capsules \subseteq Sources /\ readback \subseteq Sources
          /\ sendCounts \in [Batches -> 0..2]
          /\ receiptOK \in BOOLEAN /\ authority \in BOOLEAN /\ admitted \in BOOLEAN
          /\ aggregate \in BOOLEAN /\ built \in BOOLEAN
          /\ result \in {"absent", "error", "complete"} /\ failedAt \in Stages \cup {"none", "unknown"}
ChargePreserved == everSent \subseteq charged
ReceiptBeforeUse == (stage = "POSTCHECK" => receiptOK)
CapsuleComplete == \A s \in capsules: Required(s) \subseteq validated
BuildAfterAll == built => (aggregate /\ readback = Sources)
CompletionBound == result = "complete" => (aggregate /\ validated = Batches /\ readback = Sources
                                            /\ (~BuildRequired \/ built))
AdmissionObserved == stage = "OBSERVE" => admitted
NoReplay == \A b \in Batches: sendCounts[b] <= 1
NotCompleteWitness == result # "complete"
NotPartialFailureWitness == ~(stage = "FAILED" /\ readback = {1} /\ 3 \in charged)
NotUnknownWitness == failedAt # "unknown"
NotInterruptedWitness == ~(stage = "INTERRUPTED" /\ sent # {})
NotBuildFailureWitness == ~(stage = "FAILED" /\ failedAt = "BUILD")
=============================================================================
