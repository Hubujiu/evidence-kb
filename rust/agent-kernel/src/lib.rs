//! Pure lifecycle transitions. The host owns capabilities, persistence and admission.

use std::panic::{AssertUnwindSafe, catch_unwind};

pub const ABI_VERSION: u32 = 1;

pub mod phase {
    pub const READY: u32 = 0;
    pub const WAITING: u32 = 1;
    pub const COMPLETED: u32 = 2;
    pub const CANCELLED: u32 = 3;
    pub const FAILED: u32 = 4;
    pub const EXPIRED: u32 = 5;
    pub const LIMIT: u32 = 6;
}

pub mod operation {
    pub const NONE: u32 = 0;
    pub const MODEL: u32 = 1;
    pub const TOOL: u32 = 2;
    pub const HOST: u32 = 3;
}

pub mod event {
    pub const DISPATCH: u32 = 1;
    pub const SETTLE: u32 = 2;
    pub const FINISH: u32 = 3;
    pub const CANCEL: u32 = 4;
    pub const FAIL: u32 = 5;
}

pub mod status {
    pub const OK: u32 = 0;
    pub const INVALID_CONFIG: u32 = 1;
    pub const INVALID_STATE: u32 = 2;
    pub const INVALID_EVENT: u32 = 3;
    pub const INVALID_KIND: u32 = 4;
    pub const INVALID_ID: u32 = 5;
    pub const TERMINAL: u32 = 6;
    pub const PENDING: u32 = 7;
    pub const NO_PENDING: u32 = 8;
    pub const CORRELATION: u32 = 9;
    pub const CLOCK_REGRESSION: u32 = 10;
    pub const DEADLINE: u32 = 11;
    pub const OPERATION_LIMIT: u32 = 12;
    pub const PANIC: u32 = 13;
}

/// Caller-owned snapshot. IDs count dispatches within one run, not physical requests.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct State {
    pub abi_version: u32,
    pub phase: u32,
    pub pending_kind: u32,
    pub reserved: u32,
    pub max_operations: u64,
    pub operations_started: u64,
    pub pending_id: u64,
    pub deadline_ms: u64,
    pub last_now_ms: u64,
}

/// `now_ms` is elapsed time from the host's monotonic run clock.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Event {
    pub kind: u32,
    pub operation_kind: u32,
    pub operation_id: u64,
    pub now_ms: u64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Transition {
    pub state: State,
    pub status: u32,
    pub reserved: u32,
}

impl Transition {
    fn new(state: State, status: u32) -> Self {
        Self {
            state,
            status,
            reserved: 0,
        }
    }
}

fn valid_operation(kind: u32) -> bool {
    matches!(kind, operation::MODEL | operation::TOOL | operation::HOST)
}

fn valid_state(state: &State) -> bool {
    if state.abi_version != ABI_VERSION
        || state.reserved != 0
        || state.max_operations == 0
        || state.deadline_ms == 0
        || state.operations_started > state.max_operations
    {
        return false;
    }
    if state.phase == phase::EXPIRED {
        if state.last_now_ms < state.deadline_ms {
            return false;
        }
    } else if state.last_now_ms >= state.deadline_ms {
        return false;
    }
    match state.phase {
        phase::WAITING => {
            valid_operation(state.pending_kind)
                && state.pending_id != 0
                && state.pending_id == state.operations_started
        }
        phase::READY | phase::COMPLETED | phase::CANCELLED | phase::FAILED | phase::EXPIRED => {
            state.pending_kind == operation::NONE && state.pending_id == 0
        }
        phase::LIMIT => {
            state.pending_kind == operation::NONE
                && state.pending_id == 0
                && state.operations_started == state.max_operations
        }
        _ => false,
    }
}

fn event_shape(event: &Event) -> Result<(), u32> {
    match event.kind {
        event::DISPATCH | event::SETTLE => {
            if !valid_operation(event.operation_kind) {
                return Err(status::INVALID_KIND);
            }
            if event.operation_id == 0 {
                return Err(status::INVALID_ID);
            }
        }
        event::FINISH | event::CANCEL | event::FAIL => {
            if event.operation_kind != operation::NONE {
                return Err(status::INVALID_KIND);
            }
            if event.operation_id != 0 {
                return Err(status::INVALID_ID);
            }
        }
        _ => return Err(status::INVALID_EVENT),
    }
    Ok(())
}

fn stop(mut state: State, phase: u32, now_ms: u64, status: u32) -> Transition {
    state.phase = phase;
    state.pending_id = 0;
    state.pending_kind = operation::NONE;
    state.last_now_ms = now_ms;
    Transition::new(state, status)
}

pub fn transition(state: State, event: Event) -> Transition {
    if !valid_state(&state) {
        return Transition::new(state, status::INVALID_STATE);
    }
    if let Err(code) = event_shape(&event) {
        return Transition::new(state, code);
    }
    if state.phase >= phase::COMPLETED {
        return Transition::new(state, status::TERMINAL);
    }
    if event.now_ms < state.last_now_ms {
        return Transition::new(state, status::CLOCK_REGRESSION);
    }
    match event.kind {
        event::DISPATCH => {
            if state.phase == phase::WAITING {
                return Transition::new(state, status::PENDING);
            }
            if state.operations_started < state.max_operations
                && state.operations_started.checked_add(1) != Some(event.operation_id)
            {
                return Transition::new(state, status::INVALID_ID);
            }
        }
        event::SETTLE => {
            if state.phase != phase::WAITING {
                return Transition::new(state, status::NO_PENDING);
            }
            if event.operation_id != state.pending_id || event.operation_kind != state.pending_kind
            {
                return Transition::new(state, status::CORRELATION);
            }
        }
        event::FINISH if state.phase == phase::WAITING => {
            return Transition::new(state, status::PENDING);
        }
        _ => {}
    }
    if event.now_ms >= state.deadline_ms {
        return stop(state, phase::EXPIRED, event.now_ms, status::DEADLINE);
    }
    let mut next = state;
    next.last_now_ms = event.now_ms;
    match event.kind {
        event::DISPATCH => {
            if next.operations_started == next.max_operations {
                return stop(next, phase::LIMIT, event.now_ms, status::OPERATION_LIMIT);
            }
            next.operations_started += 1;
            next.pending_id = event.operation_id;
            next.pending_kind = event.operation_kind;
            next.phase = phase::WAITING;
        }
        event::SETTLE => {
            next.phase = phase::READY;
            next.pending_id = 0;
            next.pending_kind = operation::NONE;
        }
        event::FINISH => next.phase = phase::COMPLETED,
        event::CANCEL => return stop(next, phase::CANCELLED, event.now_ms, status::OK),
        event::FAIL => return stop(next, phase::FAILED, event.now_ms, status::OK),
        _ => return Transition::new(state, status::INVALID_EVENT),
    }
    Transition::new(next, status::OK)
}

#[unsafe(no_mangle)]
pub extern "C" fn agent_kernel_abi_version() -> u32 {
    ABI_VERSION
}

#[unsafe(no_mangle)]
pub extern "C" fn agent_kernel_new(max_operations: u64, deadline_ms: u64) -> Transition {
    let state = State {
        abi_version: ABI_VERSION,
        phase: phase::READY,
        pending_kind: operation::NONE,
        reserved: 0,
        max_operations,
        operations_started: 0,
        pending_id: 0,
        deadline_ms,
        last_now_ms: 0,
    };
    Transition::new(
        state,
        if max_operations == 0 || deadline_ms == 0 {
            status::INVALID_CONFIG
        } else {
            status::OK
        },
    )
}

#[unsafe(no_mangle)]
pub extern "C" fn agent_kernel_apply(state: State, event: Event) -> Transition {
    catch_unwind(AssertUnwindSafe(|| transition(state, event)))
        .unwrap_or_else(|_| Transition::new(state, status::PANIC))
}

/// Returns an owned, NUL-terminated JSON response. Release it with agent_harness_free.
///
/// # Safety
/// A non-null input must point to `length` readable bytes for the duration of this call.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn agent_harness_call(
    input: *const u8,
    length: usize,
) -> *mut std::ffi::c_char {
    let response = catch_unwind(AssertUnwindSafe(|| {
        if input.is_null() || length > agent_harness::MAX_REQUEST_BYTES {
            return b"{\"ok\":false,\"error\":{\"code\":\"invalid_buffer\",\"message\":\"invalid request buffer\"}}".to_vec();
        }
        let request = unsafe { std::slice::from_raw_parts(input, length) };
        agent_harness::call(request).to_string().into_bytes()
    })).unwrap_or_else(|_| b"{\"ok\":false,\"error\":{\"code\":\"panic\",\"message\":\"native harness panicked\"}}".to_vec());
    // JSON escapes all interior NUL characters; both fallback literals are NUL-free.
    std::ffi::CString::new(response)
        .expect("JSON has no interior NUL")
        .into_raw()
}

/// # Safety
/// `response` must be null or an unfreed pointer returned by agent_harness_call.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn agent_harness_free(response: *mut std::ffi::c_char) {
    if !response.is_null() {
        drop(unsafe { std::ffi::CString::from_raw(response) });
    }
}

#[cfg(test)]
mod tests;
