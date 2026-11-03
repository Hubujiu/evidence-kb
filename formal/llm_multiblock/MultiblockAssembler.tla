------------------------ MODULE MultiblockAssembler ------------------------
EXTENDS Naturals, Sequences, FiniteSets
CONSTANT Mutation
VARIABLES filled, kind, ids, valid, closed, order, terminal, rejected, rejectedBefore,
          output, replay, projected, badLength
vars == <<filled, kind, ids, valid, closed, order, terminal, rejected, rejectedBefore,
          output, replay, projected, badLength>>
Indexes == {1, 2}
Init == /\ filled = [i \in Indexes |-> FALSE] /\ kind = [i \in Indexes |-> "none"] /\ ids = [i \in Indexes |-> 0]
        /\ valid = [i \in Indexes |-> FALSE] /\ closed = [i \in Indexes |-> FALSE]
        /\ order = <<>> /\ terminal = "none" /\ rejected = FALSE
        /\ rejectedBefore = <<filled,kind,ids,valid,closed,order>> /\ output = <<>> /\ replay = <<>>
        /\ projected = FALSE /\ badLength = FALSE
Active == {i \in Indexes : kind[i] # "none"}
ValidTools == /\ \A i \in Active : kind[i] = "tool" => (ids[i] # 0 /\ valid[i])
              /\ \A i,j \in Active : (i # j /\ kind[i] = "tool" /\ kind[j] = "tool") => ids[i] # ids[j]
HasTools == \E i \in Active : kind[i] = "tool"
Start(i,k) == /\ terminal = "none" /\ kind[i] = "none"
              /\ kind' = [kind EXCEPT ![i] = k] /\ order' = Append(order,i)
            /\ rejected' = FALSE
              /\ UNCHANGED <<filled,ids,valid,closed,terminal,rejectedBefore,output,replay,projected,badLength>>
Tool(i,id) == /\ terminal = "none" /\ kind[i] = "tool" /\ ~closed[i]
              /\ ids[i] \in {0,id} /\ (~valid[i] \/ ids[i] = 0)
              /\ ids' = [ids EXCEPT ![i] = id] /\ valid' = [valid EXCEPT ![i] = TRUE]
            /\ rejected' = FALSE
              /\ UNCHANGED <<filled,kind,closed,order,terminal,rejectedBefore,output,replay,projected,badLength>>
Delta(i) == /\ terminal = "none" /\ kind[i] \in {"text","reasoning"}
            /\ ~closed[i] /\ ~filled[i] /\ filled' = [filled EXCEPT ![i] = TRUE]
            /\ rejected' = FALSE
            /\ UNCHANGED <<kind,ids,valid,closed,order,terminal,rejectedBefore,output,replay,projected,badLength>>
Close(i) == /\ terminal = "none" /\ i \in Active /\ ~closed[i]
            /\ closed' = [closed EXCEPT ![i] = TRUE]
            /\ rejected' = FALSE
            /\ UNCHANGED <<filled,kind,ids,valid,order,terminal,rejectedBefore,output,replay,projected,badLength>>
Reject(i) == /\ terminal = "none" /\ i \in Active /\ ~rejected
             /\ rejected' = TRUE
             /\ rejectedBefore' = <<filled,kind,ids,valid,closed,order>>
             /\ kind' = (IF Mutation = "rollback"
                          THEN [kind EXCEPT ![i] = IF @ = "text" THEN "reasoning" ELSE "text"]
                          ELSE kind)
             /\ UNCHANGED <<filled,ids,valid,closed,order,terminal,output,replay,projected,badLength>>
Finish(reason) == /\ terminal = "none"
                  /\ (reason \notin {"stop","tools"} \/
                      ((ValidTools \/ Mutation = "duplicate") /\ ((reason = "tools") = HasTools)))
                  /\ terminal' = reason
            /\ rejected' = FALSE
                  /\ UNCHANGED <<filled,kind,ids,valid,closed,order,rejectedBefore,output,replay,projected,badLength>>
Keep(i) == CASE terminal = "tools" -> TRUE
                [] terminal \in {"stop","max"} -> kind[i] # "tool"
                [] OTHER -> (kind[i] \in {"text","reasoning"} /\ filled[i])
Kept == SelectSeq(order, LAMBDA i: Keep(i))
Project(mismatch) == /\ terminal # "none" /\ ~projected
                    /\ output' = Kept /\ badLength' = mismatch /\ projected' = TRUE
                    /\ replay' = (IF terminal \in {"error","abort"} THEN <<>>
                                  ELSE IF mismatch /\ Mutation # "length" THEN <<>>
                                  ELSE IF Mutation = "mask" THEN order ELSE Kept)
            /\ rejected' = FALSE
                    /\ UNCHANGED <<filled,kind,ids,valid,closed,order,terminal,rejectedBefore>>
Next == (\E i \in Indexes,k \in {"text","reasoning","tool"}: Start(i,k))
        \/ (\E i \in Indexes,id \in {1,2}: Tool(i,id))
        \/ (\E i \in Indexes: Delta(i)) \/ (\E i \in Indexes: Close(i)) \/ (\E i \in Indexes: Reject(i))
        \/ (\E r \in {"stop","tools","max","error","abort"}: Finish(r))
        \/ (\E mismatch \in BOOLEAN: Project(mismatch))
TypeOK == /\ filled \in [Indexes -> BOOLEAN] /\ kind \in [Indexes -> {"none","text","reasoning","tool"}]
          /\ ids \in [Indexes -> 0..2] /\ valid \in [Indexes -> BOOLEAN]
          /\ closed \in [Indexes -> BOOLEAN] /\ order \in Seq(Indexes) /\ Len(order) <= 2
          /\ terminal \in {"none","stop","tools","max","error","abort"}
          /\ rejected \in BOOLEAN 
          /\ projected \in BOOLEAN /\ badLength \in BOOLEAN
          /\ output \in Seq(Indexes) /\ replay \in Seq(Indexes)
UniqueIndexes == Cardinality(Active) = Len(order)
RejectedUnchanged == rejected => <<filled,kind,ids,valid,closed,order>> = rejectedBefore
ExecutableUnique == terminal = "tools" => ValidTools
ReplayAligned == projected /\ ~badLength /\ terminal \notin {"error","abort"} => replay = output
ReplayLengthGuard == projected /\ badLength => replay = <<>>
SafeFailure == terminal \in {"error","abort"} => replay = <<>>
WitnessDistinctTools == ~(terminal = "tools" /\ Cardinality(Active) = 2 /\ ids[1] # ids[2])
WitnessFiltered == ~(projected /\ ~badLength /\ terminal = "max" /\ Len(order) = 2 /\ Len(output) = 1)
WitnessRejectedClose == ~(rejected /\ (\E i \in Active: closed[i]))
WitnessReordered == ~(projected /\ order = <<2,1>> /\ terminal = "tools")
Spec == Init /\ [][Next]_vars
=============================================================================
