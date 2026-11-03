# LLM stream terminal knowledge and cleanup ownership

## Concrete defect and mature references

Base `c8ef7582f226c07f1c06d3874dba159708ca7fad` retains usage in a shared
`EventSink` observation independently of the consumer queue. `LlmStream::next`
only copied that observation into the assembler when the incoming frame itself
was `Finish`. A content validation error creates a different, synthetic terminal;
a validated usage snapshot already accepted by the sink could disappear from
that response, or be replaced by an older consumed snapshot.

The existing pinned [DSH LLM assembler](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm/src/assembler.ts)
stores the latest usage independently of content and interruption filtering.
The [same-version runtime](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm/src/index.ts)
turns adapter failures into terminal chunks without clearing that prior knowledge.
Its assembler is permissive about malformed frames, unlike this project's strict
Rust boundary: this fix does not import that permissiveness. The inspected Git
blobs are `917dd11e2cfc13646ef17f037f30b5c95bd00d19` (assembler) and
`14be9c87f84925959cc3bda9004a35bc7604007f` (runtime), matching the existing
`config/plugin-runtime/dsh-llm-reference-provenance.json`. No upstream application
or paid provider was run.

[Vendored Cordis fiber](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/vendor/cordis/src/fiber.ts)
keeps lifecycle cleanup separate from superseded work and waits for dependents.
Its inspected SHA-256 `b64c3a97f15f0381479dcf3d25965e56dc4cecfa4a0881fc055a49f65b9945df`
matches the pinned source lock. The local Rust SDK already retains provider task
ownership after a terminal frame; this fix preserves that existing mechanism.

The repair freezes `terminal_usage` at the common final-publication branch, after
normal or synthetic termination, and projects that snapshot onto the response.
The snapshot is the latest validated sink observation acquired under its lock at
that boundary, not an estimate of future provider cost. Unknown stays `None`;
raw counters replace, never add; absent total tokens remain absent. Later
observations do not rewrite the already-published terminal response. No admission,
retry, routing, or actual provider cleanup operation is added or released here.
Content admission remains bounded and transactional; preserving small usage
metadata does not admit rejected content.

## Implementation evidence

Three actual public Rust SDK regressions use `Notify`, not sleeps, to establish
producer-before-consumer order:

- Duplicate block start followed by accepted usage and finish: retain known usage,
  including replacement by a smaller cumulative snapshot, keep one terminal and
  one provider invocation, and preserve unknown when no usage was observed.
- Fill the real serialized assembler exactly to its 2 MiB limit. Both another
  content delta and an assembler usage push are rejected. The stream still keeps
  sink-observed usage at the synthetic error boundary, without admitting the
  overflowing content.
- A synthesized error can publish its known snapshot while provider cleanup is
  still blocked. `close(Duration::ZERO)` returns `LLM_CLEANUP_PENDING`; after an
  explicit release the task finishes. A later observation cannot rewrite the
  frozen response.

`baseline-regression.log` and `baseline-content-limit.log` are real exit-101
counterexamples on the old implementation. `rust-tests-final.log` contains all
19 public SDK and two registry tests passing on the repaired implementation.
These are local synthetic adapters, not model, embedding, judge, HTTP or billing
calls. The temporary external consumer was only a read-only diagnostic; the
regressions above are now mandatory crate tests.

## Source-to-model map

| Model state/action | Actual owner/boundary |
| --- | --- |
| `lease`, `Dispatch`, `TaskComplete` | `Prepared::admit`, spawned provider future, `drop(owner)` after actual provider future returns |
| `queue`, `Emit`, `providerFinished` | serialized `EventSink::send`, bounded Tokio channel and terminal flag |
| `observed` | latest validated `EventSink.observed_usage` snapshot |
| `block`, `room`, `assembled`, `Consume` | `Assembler::push` clone/validate/commit and bounded content/usage acceptance |
| `terminal`, `knownAtTerminal`, `published` | common `LlmStream::next` terminal branch and `response` projection |
| `Cancel`, `SyntheticFinish` | cancellation, EOF and validation-error terminal paths |
| `Close`, `closed` | timeout/actual task join in `LlmStream::close` |

The block domain abstracts absent/text/incomplete-tool/complete-tool and content
room abstracts the real byte budget. At most four emitted frames, queue capacity
two, one admitted call, one dispatch and two symbolic nonmonotone usage snapshots
are checked. Capacity two is deliberately smaller than the implementation's 16;
this is bounded ownership/knowledge reasoning, not a queue-throughput proof.
The observed snapshot identity is not a token count, dollar cost, commit outcome,
or proof that a model request was sent. No automatic replay is modeled or added.

The positive profile checks typed state, preservation of knowledge at terminal,
no early admission release, no false cleanup, no executable partial tool, no
fabricated preterminal publication, and one terminal publication. Terminal snapshot
immutability follows the modeled guarded publication transition; no temporal
progress property is claimed. There is
no liveness/fairness assumption that arbitrary provider code will finish. A
pending close is a valid outcome, not modeled success.

The legacy negative profile restores usage capture only for incoming `Finish`
and must fail `NoLostKnownUsage`. The independent seeded negative releases the
lease at terminal publication and must fail `NoPrematureRelease`. Parse errors
and incomplete successors are not successful negative controls: initial model
construction diagnostics are retained with their explicit `initial-*` names.

## Mandatory structural guard and boundaries

`registry_violations` invokes `llm_stream_lint.stream_source_violations` for this
modeled owner, so both ordinary `formal-lint` and `--formal-only` enforce more than
source hashes. The common publication branch, snapshot replacement, actual task
join/ownership and content admission structures are checked. Discovered contracts
reject seven mutants and prove a refreshed source hash still cannot hide removal
of the terminal snapshot. This reviewed structural check is not a Rust parser or
a full refinement proof; source edits still require review and regression tests.

Remaining obligations include arbitrary block indexes, replay metadata alignment,
all malformed DTO/numeric domains, mutex poisoning, concurrent late provider
observations, multiroute lifecycle and unbounded streams. No live-provider,
operating-system, price, or whole-implementation correctness claim follows from
TLC. `llm_registry` and `host_process` remain explicitly unmodeled in this batch.

Pinned TLC is `tla2tools-1.7.4.jar`, SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`, on Java 21.
Formal and ordinary unified checker results for the final frozen tree are reported
separately; focused tests do not replace either required entrypoint.

Current retained model evidence is `tlc-one-terminal.log` (19,166 generated,
8,404 distinct states, depth 13) and `tlc-final-Legacy.log` /
`tlc-final-EarlyRelease.log` (each the intended named-invariant exit 12). Earlier
positive logs are development history, not substitutes for the current profile.
