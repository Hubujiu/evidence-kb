# LLM registry generations and managed cleanup

This is a model/test/source-guard batch, not a production bug fix. The inspected
base is `c8ef7582f226c07f1c06d3874dba159708ca7fad`. Both negative controls are
explicitly seeded incorrect variants; neither is claimed to reproduce an
unfixed registry bug. The separate LLM stream usage repair is not bundled here.

## Mature references and actual protocols

The pinned [DSH LLM runtime](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm/src/index.ts)
keeps prepared metadata and dispatch bound to the same registration and makes a
prepared call single-use. Its inspected Git blob is
`14be9c87f84925959cc3bda9004a35bc7604007f`, matching the existing provenance file.
This project is deliberately stricter about stale preparations: `ensure_current`
and `admit` refuse an old binding instead of letting a captured old registration
dispatch after replacement.

The pinned [vendored Cordis fiber](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/fiber.ts)
retains dependency ownership through cleanup; its SHA-256
`b64c3a97f15f0381479dcf3d25965e56dc4cecfa4a0881fc055a49f65b9945df` matches the
existing source lock. The local registry's retained `CleanupFailed` entry and
explicit retry are an intentional stricter policy, not a claim that all Cordis
cleanup behavior is identical.

There are two actual frontend protocols over the generic Rust registry:

- Native `Service` unregister uses `detach`: stop admitting that route, but retain
  an old entry while Admissions exist. Prepared and admitted snapshots themselves
  retain `Arc` payload ownership. Removing an entry is not destroying its payload.
- Python `ModelService` uses the native executor's begin/finish cleanup and close
  protocol. Its adapter cleanup runs outside its registry lock. A failure is
  acknowledged as `CleanupFailed`, with the owned adapter available for retry.

The models intentionally do not combine arbitrary public `Registry` methods into
an invented single frontend. In particular, unmanaged `detach` is not exposed by
the Python executor. Arbitrary mixtures of detach and managed cleanup remain an
explicit unmodeled obligation.

## Source-to-model map

| Model action/state | Actual owner |
| --- | --- |
| `Prepare`, `Admit`, ticket route/generation | `Registry::prepare`, `Inner::current`, consuming `Prepared::admit` |
| `Complete`, `Release` | executor `CallComplete` keeps its `Admission`; `CallRelease` removes it |
| `Adopt`, `generation` distinct from registration | `Registry::adopt` updates an entry's generation/identity without changing its map key |
| `Detach`, `SnapshotRefs`, `Destroy` | route withdrawal, detached active entry retention, snapshot `Arc` ownership |
| `BeginCleanup`, cleaners | native Cleaning guard and `ModelService._dispose` claim before actual adapter close |
| `CleanupFails`, `CleanupSucceeds` | finish-cleanup acknowledgement and retained failure/removal |
| `BeginClose`, `FinishClose` | service admission fence, cleanup snapshot, empty-registry acknowledgement |

### Finite bounds and claims

`LlmRegistryGenerations` uses one route name, two registration IDs, three
adoption/replacement generation values, two tickets, one adoption and one
replacement. It checks exact-generation admission, live admission ownership,
current route ownership and prepared/admitted payload references. A completed
result remains a live Admission until release. The model assumes the language's
correct `Arc` ownership and nonpoisoned mutex; no destructor scheduling liveness
is claimed.

`LlmRegistryCleanup` uses one fixed route, one call, two cleanup claimants, two
cleanup attempts and at most one failure. It checks exclusive cleanup, no cleanup
with an admitted/completed call, no unacknowledged removal and retained failure.
It does not model route-name reuse or multiroute close. The real native test and
structural guard additionally check registration refusal after close begins.
An explicit retry may physically call cleanup again: this is not exactly-once
cleanup or an idempotency promise.

The generation seeded profile bypasses current-binding validation and must
violate `NoMixedGeneration`; the cleanup seeded profile permits a second claimant
and must violate `ExclusiveCleanup`. Parse errors, timeouts, and arbitrary test
failures are not accepted as these negative controls. Both owners remain partial;
there is no all-registry refinement, provider billing, OS cleanup or unbounded
concurrency proof.

## Actual regressions and mandatory source guard

Three additional Rust registry tests execute the real public registry:

1. Adoption changes generation but not registration; stale prepared admission and
   cleanup authority fail, while a retained prepared snapshot keeps its payload
   alive after registry withdrawal.
2. Dropping a detached old Admission cannot remove or close its replacement.
3. A Barrier-controlled payload cleanup runs while a second claimant is refused;
   close fences new registration; actual fixture cleanup failure keeps the same
   entry, and an explicit retry succeeds. No ordering is inferred from sleeps.

All five registry tests passed, including the existing reentrant-destructor and
admission-drain tests. The first targeted compile hit tmpfs ENOSPC before any test
ran; `registry-tests-enospc.*` preserves that failure. Only this worker's completed
incremental build caches were reclaimed, then its build target moved to its own
workspace. The successful compile/test log is separate.

`registry_violations` invokes the structural guard for this modeled owner, making
it part of ordinary `formal-lint` and the additional formal entrypoint. It reviews
exact binding before admission, consuming prepared tickets, result-versus-release,
cleanup exclusivity/retained failure, destruction outside the lock and the Python
claim/close/acknowledge ordering. Discovered contracts reject nine seeded source
mutants and demonstrate that refreshing hashes cannot hide a generation-check
regression. The guard is bounded structural/AST checking, not a full Rust parser
or a machine-checked source refinement proof.

Pinned TLC uses Java 21 and `tla2tools-1.7.4.jar` SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
Current direct logs are `LlmRegistryGenerations-final.log` and
`LlmRegistryCleanup-final.log`, plus their `Seeded-final` counterparts. Full
ordinary and formal-only checker results for the frozen commit are reported
separately; focused tests do not replace those entrypoints. No paid or live
provider, embedding, judge or external database calls were made.
