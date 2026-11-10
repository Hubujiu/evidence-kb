# Nested request ownership

`NestedRequestOwnership` joins ToolExecutor callback retry, remote provider permit ownership, physical request receipts and conservative budget reservations. The existing local models retain circuit generation/probe, transport deadline and ledger details.

A logical callback acquires one permit before sending. Up to two physical attempts may occur under that permit; a read-only outer retry acquires a new permit. Unknown outcomes prevent retry and retain the physical reservation. Reusing a terminal tool result cannot start new work. Physical attempt identities are represented by a monotonically increasing bounded index.

Bounds: two outer callbacks, two inner attempts each, four physical identities, sequential execution. Circuit cooldown is abstracted as eligibility, not a clock proof. ProviderCircuit's separate model supplies generation and half-open ownership assumptions. The model does not claim arbitrary concurrent callback composition, monetary estimation correctness, transport exactly-once delivery, or source refinement.

The positive configuration checks eight invariants. Four fault configurations expose unknown replay, missing admission, dropped unknown reservation, and terminal replay. Three witness configurations establish actual inner retry, outer retry, and unknown terminal paths by intentionally violating a negated reachability predicate. All controls require their precise named invariant, not any error.

Source mapping: `ToolExecutor.execute` owns logical attempts; `remote._provider_guard` owns circuit acquire/finish; RequestReceipts owns physical identity; BudgetLedger owns charges. Executable offline composition contracts and AST mutation checks accompany the model. The unified mandatory checker executes these with all prior obligations.
