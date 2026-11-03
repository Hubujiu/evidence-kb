# Python host ownership and HTTP lifespan

This slice complements PR57's Rust coordinator rather than treating native state
as a proof of the Python executor. It has four independently configured finite
models and mandatory source-shape guards whose negative tests refresh hashes.

## Mature references and actual fixes

* Python's [contextmanager resource pattern](https://docs.python.org/3/library/contextlib.html#contextlib.contextmanager)
  places fallible work after acquisition inside a releasing `finally`.
  `PluginHost.lease` and `PluginContext.lease` previously installed their ContextVar
  before entering that boundary. A controlled allocation failure in `set` left a
  real native lease alive; nesting also retained an extra local `users` count.
  The fix extends the existing boundary over context installation and resets only
  a successfully installed token. It does not add a new lease framework.
* [FastAPI's own lifespan implementation](https://github.com/fastapi/fastapi/blob/master/fastapi/routing.py)
  retains its legacy `_shutdown` dispatcher after Starlette removed `shutdown`.
  The locked dependencies are FastAPI 0.156.4 and Starlette 1.9.0. Inspection of
  the installed locked source confirmed `_DefaultLifespan.__aexit__` delegates to
  `APIRouter._shutdown`, which runs sync/async registered shutdown handlers.
  The old never-run/startup-failed and API/delivery construction-failed paths
  called nonexistent `router.shutdown`, raising AttributeError without invoking
  the resource handlers. The three fallback sites now use the same dispatcher
  as FastAPI's default lifespan. This is an intentional private-API dependency
  on the locked framework, guarded by real-framework integration tests and
  mandatory lint. Dependency upgrades must rerun those contracts. Entering a
  fresh lifespan merely to close would wrongly execute startup; duplicating the
  framework's handler dispatcher would create an unnecessary compatibility layer.
* Python [Timer cancellation and join](https://docs.python.org/3/library/threading.html#timer-objects)
  distinguish cancelling a waiting timer from waiting for an already executing
  callback. The existing invoke cleanup's cancel/join ordering is retained.

## Model-to-source map

* `PythonLease`: acquisition, failing context setup, nested users, last-user
  settlement, invocation signal registration and timer callback/join before return.
  Two nested users, one invocation, one timer and one settlement. Fresh host and
  instance leases share this acquisition pattern; native identity validation stays
  in the separately checked native model.
* `PythonPump`: two contenders sharing one root token, init sealing, one disposer,
  failed callback retention, explicit retry, collection and cached final snapshot.
  Callback bodies and native completion/ack are atomic abstract interfaces, not
  a proof that arbitrary callback effects are transactional or exactly once.
* `PythonConfiguration`: an off-lock validation races a concurrent config update;
  entry-identity recheck prevents committing stale input. Native update and Python
  entry publication use the same RLock. It abstracts payloads by opaque revisions.
* `HttpLifespan`: ready/serving/run-ended/closed, entry failure, successful or
  failing attempted exit, never-run fallback, repeated close and fallback failure.
  `ApplicationWorker` calls close after run settles, or before it starts. Concurrent
  close during serve is not an invented supported operation. `stop` is a separate
  idempotent request, not proof the server is already stopped.

Four positive configurations have fourteen deliberate unsafe variants and six
non-Init reachability witnesses. Witnesses intentionally violate a named
`...Unreachable` invariant; they are not production bugs. Ordinary contracts use
real native leases plus controlled setup failures. Existing deterministic runtime
race contracts continue to cover callback lock release, revalidation, cancellation
registration and timer setup failures.

HTTP async cases live in the mandatory foundation integration suite, not sealed
contract workers: an event-loop self-pipe on Windows is real local OS activity.
No audit guard is disabled. Real FastAPI routers verify both sync and async
handlers, no startup-on-close, entry failure and both actual factory cleanup paths.

## Boundaries

These are bounded safety models plus executable source guards and regressions,
not a refinement proof or a universal absence-of-bugs claim. External native/OS/
ASGI correctness, arbitrary process kill and permanently failing disposers remain
assumptions or separate owners. An attempted lifespan exit marks the HTTP wrapper
finished even if the exit reports failure: this avoids silently rerunning arbitrary
shutdown effects and does not assert that every resource closed. A failed fallback
remains explicit and retryable, matching the existing behavior. Metadata snapshots
are copied on read; collection needs native terminal eligibility, not a promise of
instantaneous revocation. Original test/model logs remain private; no raw execution
logs or local diagnostic paths are published in this change.

Context tokens are exited on the same thread/context that entered them. The lease
fix covers failure while installing context after successful acquisition, not
arbitrary allocation failure before the local lease object exists or failures in
native release. Token reset is assumed to obey Python's valid same-context token
contract. The split acquire/publication critical sections remain pinned by the
native lease; a deterministic concurrent-close contract verifies refusal before
publication and retention of contexts. A factory construction failure followed by
shutdown failure propagates the latter with the constructor failure in Python's
exception context; it does not preserve the constructor as the top-level error.

### Process dispatcher observation boundary

The local process-supervision test previously waited only for transport close and the
first dependent disposer event. An actual native-completion barrier reproduced a
`cleaning` snapshot with one queued action even though that predicate was true.
Releasing the barrier lets the original pump settle to `pending` with zero lease,
resource, operation, pending-operation and queue counts, without an external close
or process retry. `pending` is a valid live host state, not a disposed business state.
The fixture now waits for all original teardown assertions (plus operation counts)
within the unchanged three-second deadline. Mandatory controlled process cases
release the barrier before waiting (PASS) or hold it through that deadline (correct
FAIL), then observe natural dispatcher settlement before cleanup. This corrects the
test's observation boundary; it is not a production process-supervision fix.
The protocol follows Python `threading.Event` semantics: an event signals its own
operation, not completion of unrelated work after `set()`; completion must be
observed at the owning dispatcher. AST guards and refreshed-hash mutations reject
restoring the early predicate, omitting the queue, or bypassing the barrier gate.


### Nested invocation reachability correction

The nested witness previously required `Nest` but could reach settlement directly
through `Finish`, without executing `Invoke`: that action also stops an absent
timer. Following the explicit transition/history distinction in
[Lamport's Specifying Systems, chapter 8](https://lamport.azurewebsites.net/tla/book-02-08-08.pdf),
`invokedSeen` is initialized false and set only by the actual `Invoke` action.
The witness now requires both real nesting and real invocation before settlement.
Normal release without invocation remains valid. Mandatory model-shape checks and
mutants reject missing witness predicates, fabricated initial history, missing
Nest/Invoke event updates, and omitted history state. This strengthens evidence
for the existing finite `PluginHost._invoke`/lease abstraction, not a new claim
of a production bug or a source refinement proof. Existing live source contracts,
model bounds, safety conditions and execution deadlines are unchanged.
