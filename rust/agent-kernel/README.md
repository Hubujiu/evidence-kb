# Agent lifecycle kernel

The lifecycle reducer uses a fixed-value C ABI and has no knowledge of document or model payloads. This library also exports the versioned JSON bridge to the sibling `agent-harness` crate for plugin ownership and loop effects. The host owns effect execution, monotonic clock sampling, authorization, persistence and physical request accounting.

Build and verify through the project's normal `scripts/check_project.py` entry point. The standalone native commands are:

```text
cargo fmt --all -- --check
cargo build --locked --offline --package agent-kernel
cargo test --locked --offline --package agent-kernel
```

## ABI version 1

The lifecycle structures use `repr(C)` and contain fixed-width unsigned integers. These inputs and outputs are passed by value; the caller owns every snapshot. These lifecycle functions do not exchange allocations, pointers or callbacks. The separate harness JSON bridge below uses owned output buffers.

```c
struct State {
    uint32_t abi_version;
    uint32_t phase;
    uint32_t pending_kind;
    uint32_t reserved;
    uint64_t max_operations;
    uint64_t operations_started;
    uint64_t pending_id;
    uint64_t deadline_ms;
    uint64_t last_now_ms;
};
struct Event {
    uint32_t kind;
    uint32_t operation_kind;
    uint64_t operation_id;
    uint64_t now_ms;
};
struct Transition {
    struct State state;
    uint32_t status;
    uint32_t reserved;
};
uint32_t agent_kernel_abi_version(void);
struct Transition agent_kernel_new(uint64_t max_operations, uint64_t deadline_ms);
struct Transition agent_kernel_apply(struct State state, struct Event event);
```

On the supported 64-bit targets, State is 56 bytes, Event is 24 bytes and Transition is 64 bytes. `reserved` fields are zero. A bridge must check the version before constructing a snapshot. Numeric constants are declared in `src/lib.rs`.

Dispatch admits exactly one operation with ID `operations_started + 1`. A matching settlement clears that pending operation. The host only performs the operation after successful dispatch admission. Finishing while an operation is pending is rejected. Cancellation and failure clear pending work and make the run terminal; a late settlement cannot revive it. There is no automatic retry or durable replay.

`now_ms` is elapsed time measured against one host monotonic start time. A clock regression is rejected. At or after `deadline_ms`, an otherwise valid event terminates the run as expired. Reaching the operation cap still permits finishing, but a further dispatch terminates the run as limited. Deadline and operation-limit responses carry the resulting terminal snapshot; validation failures leave the input snapshot unchanged. Every event against an already terminal snapshot is rejected.

The host must keep a snapshot private to its run and correlate its own invocation handles. Kernel snapshots are not authentication tokens or evidence of durable execution. Reusing an old snapshot cannot authorize a replay in the session store or the physical request ledger.

## Harness JSON bridge

`agent_harness_call(const uint8_t *input, size_t length)` accepts at most 4 MiB of UTF-8 JSON, including `version: 1` and a command. It returns an allocated NUL-terminated JSON response. The caller must release that response exactly once with `agent_harness_free(char *output)`. Input memory remains owned by the caller. Native calls catch panics and return structured errors; they do not call Python while holding registry locks.

Plugin manifests, temporary model requests and structured tool results use this bridge. Native handles remain process-local and are explicitly closed; they are not durable recovery records. The independent `agent-harness` binary exposes the same commands over JSON-lines. See [the runtime guide](../../docs/rust-agent-runtime.md) for effects, ownership and transport examples.
