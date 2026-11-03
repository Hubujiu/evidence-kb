# Structured handle ownership: launch, queue drain, guard and bounded receipts

This is a **partial**, finite safety audit of `storage_structured_wire.py`, stacked
on PR #39's commit-knowledge change. It does not replace that model, claim all
state machines are correct, authorize replay, or establish unconditional cleanup.
No provider/model/embedding/judge calls were made. SQLite and synthetic faults are
used, not production incidents or naturally observed OS resource exhaustion.

## Mature implementations reviewed first

- [CPython 3.12.14 Thread.start](https://github.com/python/cpython/blob/v3.12.14/Lib/threading.py)
  registers a thread in its pending registry, removes that registration when
  native creation raises an ordinary `Exception`, and reraises the original
  failure. This motivates ownership rollback only for a known failed launch.
- [CPython 3.12.14 ThreadPoolExecutor](https://github.com/python/cpython/blob/v3.12.14/Lib/concurrent/futures/thread.py)
  arbitrates cancellation using `set_running_or_notify_cancel()` before work.
- [CPython 3.12.14 Future](https://github.com/python/cpython/blob/v3.12.14/Lib/concurrent/futures/_base.py)
  performs that arbitration under the Future condition; cancellation cannot win
  after RUNNING. A separate `done()` check does not protect a subsequent
  `set_exception()` from concurrent cancellation.

The project keeps stdlib threads/Futures and its existing SQLite/SQLAlchemy
managers. No replacement executor, custom Future state machine, or external code
copy was introduced. These references justify design choices, not refinement.

## Three concrete defects and fixes

1. `_open` publishes a handle before `_Handle.start`. Native thread creation
   failure could leave that never-started handle registered. Close could then
   enqueue rollback for a nonexistent worker and later join an unstarted thread.
   A separate
   `_launched` state under `_state_lock` now distinguishes no-worker ownership
   from launched-but-not-ready ownership. Known launch failure retires once with
   `UNKNOWN` and the original operation ID, then uses a bare re-raise. Unstarted
   close retires idempotently without queueing rollback or joining. A stopped
   handle cannot subsequently launch; a duplicate start cannot retire a live one.
   A ready timeout does **not** free a launched worker's slot before real cleanup.
2. The worker's final queue drain checked `not queued.done()` before publishing
   `STORAGE_HANDLE_CLOSED`. A call timeout could cancel between these operations,
   raising `InvalidStateError` inside the finalizer before `_finished`, leaking
   ownership and the terminal receipt. The drain now claims the queued Future
   atomically. If cancellation already won it skips publication; otherwise it
   publishes CLOSED on a running Future. It neither executes the queued action
   nor catches and hides `InvalidStateError`.
3. `PortableStructuredClient.guard` started the heartbeat before entering its
   cleanup `try/finally`. Thread allocation failure abandoned the already-held
   remote guard until lease expiry. Startup is now inside that existing cleanup
   region. A failed start skips join, attempts release and preserves the original
   failure even if the release acknowledgement is lost. An unsuccessful release
   is not claimed successful and does not trigger retries.

`_ready` has no cancellation path. The currently executing `pending` Future has
already passed `set_running_or_notify_cancel`, so its result publication does not
have the queued-drain cancellation race. Those unrelated branches remain intact.
Normal manager exit remains the only source of known terminal outcome; PR #39's
UNKNOWN/operation-ID boundary is unchanged.

### Lock and callback review

Launch and unstarted close serialize under `_state_lock`. Native start returns
before the worker can finish its own locked drain, and neither ready waiting nor
join occurs while that lock is held. Endpoint `_open` and `close` release the
endpoint lock before invoking handle methods. `_finished` releases the endpoint
lock before taking the transport lock. Transfer validation takes transport then
endpoint locks, but never the handle state lock. These inspected paths do not
form an inverse lock cycle. The internal completion callback is trusted; arbitrary
throwing callbacks, allocation failure inside receipt bookkeeping, and process
termination are not modeled.

## Models and source correspondence

| Model | Actions / production transition | Claims and finite bounds |
| --- | --- | --- |
| `HandleLaunch` | `Launch`/`LaunchFailure`: locked `_Handle.start`; `CloseUnstarted`/`RetryUnstartedClose`: first/second close; `Enter`/`EnterError`/`Finish`: manager and worker finally; `ReadyTimeout`: ready wait timeout | One handle/one native launch/one manager exit; failed launch retires, unstarted join forbidden, no late launch, live worker retains slot, one report, known outcome requires exit |
| `HandleQueue` | `Dispatch`: normal Future claim; `Timeout`: `call` timeout and real `cancel`; `FinishActive`: final drain admission stop; `Drain`/`Publish`: cancellation arbitration and CLOSED publication; `Release`: `_finished` | One worker, current abstract action and one queued Future, one timeout; cancellation never executes work, drain never aborts, no premature retirement |
| `GuardLease` | `StartFailure`: client heartbeat startup failure; `LeaveBody`/`Finish`/`LostFinish`: client finally and release observation; `Ping`/`Expire`: worker heartbeat/idle lease paths | One acquired guard, one heartbeat, one ping, four clock ticks; failed start cannot abandon release attempt, stopped handle cannot renew, release receipt requires actual release, primary failure retained |
| `HandleRetention` | `Open`: `_open` locked admission; `Finish`: `_finished` pop/cache/evict; `Observe`: repeated `_execute(finish)` | Three unique tokens, active capacity two, receipt retention two; bounded admission/cache, no replay or fabricated receipt, eviction returns CLOSED |

Launch models only transaction outcomes; guard RELEASED is in `GuardLease`.
Queue timeout is enabled even after the handle's stop flag is set: caller timeout
and Future cancellation are outside `_state_lock`. The old queue model control
splits check and publication to expose the cancellation window. Guard clock is an
observed live-dispatch/idle-expiry abstraction, **not** a hard wall-clock deadline:
queue arrival and thread scheduling between timeout calculation and dispatch are
not exhaustively modeled. Lost finish can leave the remote guard held and the
client result UNKNOWN. No fairness or liveness property is asserted.

Each positive configuration includes nonempty safety assertions. Seeded negatives
must violate the named invariant with TLC exit 12; parsing, incomplete states,
timeouts or unrelated errors do not count. Separate witness configurations negate
reachable fresh launch/commit, unstarted close, delayed cleanup, queue execution,
cancelled drain, heartbeat renewal/release, fresh admission and eviction. They
check that safety was not obtained merely by disabling all useful actions.

## Actual contracts and structural guardrails

- Public SQLite launch failure probe, original exception identity, fresh committed
  write, repeated failed launches without consuming reduced capacity, idempotent
  pre-launch close, Event-controlled endpoint close before published handle launch,
  duplicate start rejection, delayed real manager entry/cleanup after ready timeout.
- Actual worker plus SQLite insert/rollback with a deterministic Future timeout at
  drain publication. The Future subclass only coordinates `result` timeout and
  `set_exception`; the production `call` path performs genuine `Future.cancel`.
  The old implementation raises `InvalidStateError`; fixed cancellation returns
  false after the drain claim, the worker retires and the queued action never runs.
  The failed manager entry has UNKNOWN knowledge, despite actual SQLite rollback.
- Real queued pre-dispatch cancellation, cancelled residual drain, actual 64-entry
  queue saturation without stopping the live worker, and all 64 requests drained.
- Public guard heartbeat allocation failure, immediate reacquisition of the actual
  SQLite sidecar lock, lost release acknowledgement with original exception
  preserved. A phase-controlled queue verifies a ping renews the 60-second idle
  budget, real expiry releases exclusion, and stopped ping cannot resurrect it.
- 257 actual read-only SQLite transactions verify production 256-outcome FIFO
  eviction, exact retained receipts and no backend replay. Reduced handle capacity
  two verifies admission rejection before invoking the backend factory while live
  transactions remain owned. This is not an exhaustive 64-handle concurrency test.

`storage_handle_lint_contracts.py` is discovered by the mandatory ordinary contract
gate. Its narrow AST rules enforce launch guards, exception type and original
failure, late-ready ownership, unstarted-close return, atomic drain arbitration,
heartbeat protected startup and no unstarted join. Twelve seeded source mutants
must be rejected. Registry AST hashes bind reviewed methods; hash equality and
these syntactic rules are not Python semantic proofs.

## Explicit remaining obligations

The retention model covers transaction receipt statuses; guard RELEASED uses the
same production cache but is not a separate retention proof. Historical outcomes
in that model are a truth oracle, not an assertion that evicted server receipts
remain observable.

Open obligations: cross-model composition; all 64 queue/capacity interleavings;
unbounded/token-collision behavior; exact real-time clocks; in-flight heartbeat
and transport shutdown; context-manager suppression semantics; asynchronous
`KeyboardInterrupt` between native launch and the launch flag; adversarial
callbacks; database/OS internals; schema/scope owners. In particular an uncertain
native launch is not treated as a proven no-effect failure. No all-system or
unconditional progress claim is made.

## Verification and publication scope

This code-only review contains production changes, mandatory contracts and
structural mutants, finite models/configurations, source bindings and this normal
implementation explanation. Local raw execution logs and historical failure
snapshots are not part of this publication. No test depends on those artifacts;
the mandatory project and formal gates still execute the live contracts/models.

Use both repository-required commands with new unique output directories:

```sh
uv run --no-sync python scripts/check_project.py --output .local/checks/new-ordinary
uv run --no-sync python scripts/check_project.py --formal-only --output .local/checks/new-formal
```

The pinned Java/TLC and native build prerequisites remain unchanged. CI must check
the exact published commit; tests of another tree are not represented as an exact
published-tree pass. This review stays draft until its outstanding checks finish.
