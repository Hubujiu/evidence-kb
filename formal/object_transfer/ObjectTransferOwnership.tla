---------------------- MODULE ObjectTransferOwnership ----------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS UploadBytes, LeakFinishedHandle, ReplayAmbiguous
Handles == {"U", "D"}
LiveModes == {"upload", "download"}
VARIABLE s
vars == <<s>>
Live == {h \in Handles : s.mode[h] \in LiveModes}
Bytes(h) == IF h = "U" THEN UploadBytes ELSE 2
Capacity == (IF "U" \in Live THEN UploadBytes ELSE 0)
          + (IF "D" \in Live THEN 2 ELSE 0)
Ready == ~s.closed /\ s.lock = "none" /\ \A h \in Live : s.age[h] < 2
Init == s = [closed |-> FALSE, mode |-> [h \in Handles |-> "unused"],
  offset |-> [h \in Handles |-> 0], age |-> [h \in Handles |-> 0],
  closeAttempted |-> {}, lock |-> "none", finish |-> "none", calls |-> 0,
  physical |-> FALSE, result |-> "none", client |-> "idle", checksum |-> TRUE,
  downloadComplete |-> FALSE, closeFaultUsed |-> FALSE]

BeginUpload(good) ==
  /\ Ready /\ s.mode["U"] = "unused" /\ Cardinality(Live) < 2
  /\ Capacity + UploadBytes <= 3
  /\ s' = [s EXCEPT !.mode["U"] = "upload", !.age["U"] = 0,
                     !.checksum = good, !.client = "open"]
BeginDownload ==
  /\ Ready /\ s.mode["D"] = "unused" /\ Cardinality(Live) < 2
  /\ Capacity + 2 <= 3
  /\ s' = [s EXCEPT !.mode["D"] = "download", !.age["D"] = 0]
WriteChunk ==
  /\ Ready /\ s.mode["U"] = "upload" /\ s.offset["U"] < UploadBytes
  /\ s' = [s EXCEPT !.offset["U"] = @ + 1, !.age["U"] = 0]
ReadChunk ==
  /\ Ready /\ s.mode["D"] = "download"
  /\ LET n == IF s.offset["D"] < 2 THEN s.offset["D"] + 1 ELSE 2 IN
     s' = [s EXCEPT !.offset["D"] = n, !.age["D"] = 0,
       !.mode["D"] = IF n = 2 THEN "retired" ELSE @,
       !.closeAttempted = IF n = 2 THEN @ \cup {"D"} ELSE @,
       !.downloadComplete = n = 2]
(* Wrong mode/offset still refreshes touched; no stream progress. *)
RejectedOperation(h) ==
  /\ Ready /\ h \in Live
  /\ s' = [s EXCEPT !.age[h] = 0]
Tick ==
  /\ \E h \in Live : s.age[h] < 2
  /\ s' = [s EXCEPT !.age = [h \in Handles |->
        IF h \in Live /\ s.age[h] < 2 THEN s.age[h] + 1 ELSE s.age[h]]]
(* Expiry is lazy: a subsequent valid-shaped execute sweeps stale handles.
   All request actions require the sweep complete; malformed requests stutter. *)
Expire(h) ==
  /\ ~s.closed /\ s.lock = "none" /\ h \in Live /\ s.age[h] = 2
  /\ s' = [s EXCEPT !.mode[h] = "retired", !.closeAttempted = @ \cup {h}]
Cancel(h) ==
  /\ Ready /\ h \in Live
  /\ s' = [s EXCEPT !.mode[h] = "retired", !.closeAttempted = @ \cup {h}]
FinishBegin ==
  /\ Ready /\ s.mode["U"] = "upload" /\ s.client = "open"
  /\ s' = [s EXCEPT !.lock = "finish", !.age["U"] = 0,
        !.client = "submitted", !.finish = IF s.offset["U"] = UploadBytes /\ s.checksum
                                           THEN "put" ELSE "invalid"]
ValidationFailure ==
  /\ s.lock = "finish" /\ s.finish = "invalid"
  /\ s' = [s EXCEPT !.finish = "cleanup", !.result = "error"]
(* Driver error is allowed both before and after actual storage effect. A bad
   returned reference likewise fails verification after the object exists. *)
Put(effect, acknowledged) ==
  /\ s.lock = "finish" /\ s.finish = "put"
  /\ acknowledged => effect
  /\ s' = [s EXCEPT !.calls = @ + 1, !.physical = effect,
        !.result = IF acknowledged THEN "success" ELSE "error", !.finish = "cleanup"]
FinishCleanup ==
  /\ s.lock = "finish" /\ s.finish = "cleanup"
  /\ s' = [s EXCEPT !.mode["U"] = IF LeakFinishedHandle THEN @ ELSE "retired",
        !.closeAttempted = IF LeakFinishedHandle THEN @ ELSE @ \cup {"U"},
        !.lock = "none", !.finish = "returned"]
Observe(delivered) ==
  /\ s.finish = "returned" /\ s.client = "submitted"
  /\ s' = [s EXCEPT !.client = IF delivered /\ s.result = "success"
                              THEN "success" ELSE "error"]
ClientCancel ==
  /\ s.client \in {"success", "error"} /\ s.lock = "none"
  /\ s' = [s EXCEPT !.client = IF s.client = "success" THEN "done-success" ELSE "done-error"]
(* Deliberately unsafe mutant, never asserted to occur in the real client. *)
Replay ==
  /\ ReplayAmbiguous /\ s.client = "error" /\ s.physical /\ s.calls = 1
  /\ s' = [s EXCEPT !.calls = 2]
CloseBegin ==
  /\ s.lock = "none" /\ (~s.closed \/ Live # {})
  /\ s' = [s EXCEPT !.closed = TRUE, !.lock = "close"]
CloseOne(h, fault) ==
  /\ s.lock = "close" /\ h \in Live /\ (fault => ~s.closeFaultUsed)
  /\ s' = [s EXCEPT !.mode[h] = "retired", !.closeAttempted = @ \cup {h},
        !.lock = IF fault THEN "none" ELSE @, !.closeFaultUsed = @ \/ fault]
(* pop precedes stream.close: if cleanup raises, remaining handles survive and
   closed rejects execute; a later explicit close resumes remaining work. *)
CloseEnd ==
  /\ s.lock = "close" /\ Live = {}
  /\ s' = [s EXCEPT !.lock = "none"]
Next == (\E good \in BOOLEAN : BeginUpload(good)) \/ BeginDownload
     \/ WriteChunk \/ ReadChunk \/ Tick \/ FinishBegin \/ ValidationFailure
     \/ (\E effect, ack \in BOOLEAN : Put(effect, ack)) \/ FinishCleanup
     \/ (\E delivered \in BOOLEAN : Observe(delivered)) \/ ClientCancel \/ Replay
     \/ CloseBegin \/ CloseEnd
     \/ (\E h \in Handles : RejectedOperation(h) \/ Expire(h) \/ Cancel(h) \/ (\E fault \in BOOLEAN : CloseOne(h, fault)))
Spec == Init /\ [][Next]_vars
TypeOK == /\ s.closed \in BOOLEAN /\ s.mode \in [Handles -> {"unused", "upload", "download", "retired"}]
          /\ s.offset \in [Handles -> 0..2] /\ s.age \in [Handles -> 0..2]
          /\ s.closeAttempted \subseteq Handles /\ s.lock \in {"none", "finish", "close"}
          /\ s.finish \in {"none", "put", "invalid", "cleanup", "returned"}
          /\ s.calls \in 0..2 /\ s.physical \in BOOLEAN /\ s.checksum \in BOOLEAN
          /\ s.result \in {"none", "success", "error"}
          /\ s.client \in {"idle", "open", "submitted", "success", "error", "done-success", "done-error"}
          /\ s.downloadComplete \in BOOLEAN /\ s.closeFaultUsed \in BOOLEAN
BoundedCapacity == Cardinality(Live) <= 2 /\ Capacity <= 3
OrderedOffsets == \A h \in Handles : s.offset[h] <= Bytes(h)
FinishOwnsStream == s.lock = "finish" => s.mode["U"] = "upload"
TerminalHandleReleased == s.finish = "returned" => s.mode["U"] = "retired"
NoUnverifiedPut == s.calls > 0 => s.checksum /\ s.offset["U"] = UploadBytes
ClientSingleAttempt == s.calls <= 1
AcknowledgmentSound == s.client \in {"success", "done-success"} => s.physical /\ s.result = "success"
DownloadReleased == s.downloadComplete => s.mode["D"] = "retired" /\ "D" \in s.closeAttempted
NotSuccessfulTransfer == ~(s.client = "done-success" /\ s.downloadComplete)
NotAmbiguousEffect == ~(s.client = "done-error" /\ s.physical /\ s.mode["U"] = "retired")
=============================================================================
