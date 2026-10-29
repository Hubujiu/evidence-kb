use super::*;

fn event(kind: u32, operation_kind: u32, operation_id: u64, now_ms: u64) -> Event {
    Event {
        kind,
        operation_kind,
        operation_id,
        now_ms,
    }
}

fn initial(max_operations: u64) -> State {
    let result = agent_kernel_new(max_operations, 100);
    assert_eq!(result.status, status::OK);
    result.state
}

fn applied(state: State, event: Event) -> State {
    let result = agent_kernel_apply(state, event);
    assert_eq!(result.status, status::OK);
    result.state
}

#[test]
fn model_tool_and_host_operations_can_complete_one_run() {
    let mut state = initial(3);
    for (offset, kind) in [operation::MODEL, operation::TOOL, operation::HOST]
        .into_iter()
        .enumerate()
    {
        let id = offset as u64 + 1;
        state = applied(state, event(event::DISPATCH, kind, id, id * 2));
        assert_eq!(state.phase, phase::WAITING);
        state = applied(state, event(event::SETTLE, kind, id, id * 2 + 1));
    }
    let state = applied(state, event(event::FINISH, operation::NONE, 0, 9));
    assert_eq!(state.phase, phase::COMPLETED);
    assert_eq!(state.operations_started, 3);
}

#[test]
fn pending_operation_excludes_another_dispatch_and_finish() {
    let state = applied(initial(3), event(event::DISPATCH, operation::MODEL, 1, 1));
    for request in [
        event(event::DISPATCH, operation::TOOL, 2, 2),
        event(event::FINISH, operation::NONE, 0, 2),
    ] {
        let result = agent_kernel_apply(state, request);
        assert_eq!(result.status, status::PENDING);
        assert_eq!(result.state, state);
    }
}

#[test]
fn wrong_duplicate_or_reordered_receipts_cannot_advance_run() {
    let state = applied(initial(3), event(event::DISPATCH, operation::TOOL, 1, 1));
    for request in [
        event(event::SETTLE, operation::TOOL, 2, 2),
        event(event::SETTLE, operation::MODEL, 1, 2),
    ] {
        let result = agent_kernel_apply(state, request);
        assert_eq!(result.status, status::CORRELATION);
        assert_eq!(result.state, state);
    }
    let state = applied(state, event(event::SETTLE, operation::TOOL, 1, 2));
    let duplicate = agent_kernel_apply(state, event(event::SETTLE, operation::TOOL, 1, 3));
    assert_eq!(duplicate.status, status::NO_PENDING);
    assert_eq!(duplicate.state, state);
    let reordered = agent_kernel_apply(state, event(event::DISPATCH, operation::TOOL, 3, 3));
    assert_eq!(reordered.status, status::INVALID_ID);
    assert_eq!(reordered.state, state);
}

#[test]
fn cancellation_discards_pending_and_never_accepts_late_success() {
    let state = applied(initial(3), event(event::DISPATCH, operation::TOOL, 1, 1));
    let state = applied(state, event(event::CANCEL, operation::NONE, 0, 2));
    assert_eq!(state.phase, phase::CANCELLED);
    assert_eq!(state.pending_id, 0);
    let late = agent_kernel_apply(state, event(event::SETTLE, operation::TOOL, 1, 3));
    assert_eq!(late.status, status::TERMINAL);
    assert_eq!(late.state, state);
}

#[test]
fn deadline_blocks_dispatch_and_settlement_at_exact_boundary() {
    for state in [
        initial(3),
        applied(initial(3), event(event::DISPATCH, operation::MODEL, 1, 1)),
    ] {
        let kind = if state.phase == phase::READY {
            event::DISPATCH
        } else {
            event::SETTLE
        };
        let result = agent_kernel_apply(state, event(kind, operation::MODEL, 1, 100));
        assert_eq!(result.status, status::DEADLINE);
        assert_eq!(result.state.phase, phase::EXPIRED);
        assert_eq!(result.state.pending_id, 0);
        assert_eq!(result.state.operations_started, state.operations_started);
        let later = agent_kernel_apply(result.state, event(event::FINISH, operation::NONE, 0, 101));
        assert_eq!(later.status, status::TERMINAL);
    }
}

#[test]
fn regressing_host_clock_does_not_change_snapshot() {
    let state = applied(initial(3), event(event::DISPATCH, operation::HOST, 1, 20));
    let result = agent_kernel_apply(state, event(event::SETTLE, operation::HOST, 1, 19));
    assert_eq!(result.status, status::CLOCK_REGRESSION);
    assert_eq!(result.state, state);
}

#[test]
fn operation_limit_allows_completion_but_forbids_more_work() {
    let state = applied(initial(1), event(event::DISPATCH, operation::TOOL, 1, 1));
    let state = applied(state, event(event::SETTLE, operation::TOOL, 1, 2));
    assert_eq!(
        agent_kernel_apply(state, event(event::FINISH, operation::NONE, 0, 3)).status,
        status::OK
    );
    let exhausted = agent_kernel_apply(state, event(event::DISPATCH, operation::TOOL, 2, 3));
    assert_eq!(exhausted.status, status::OPERATION_LIMIT);
    assert_eq!(exhausted.state.phase, phase::LIMIT);
    assert_eq!(exhausted.state.operations_started, 1);
}

#[test]
fn failure_and_completion_are_terminal_even_for_control_events() {
    for finish in [event::FAIL, event::FINISH, event::CANCEL] {
        let state = applied(initial(3), event(finish, operation::NONE, 0, 1));
        for kind in [event::FAIL, event::FINISH, event::CANCEL] {
            let result = agent_kernel_apply(state, event(kind, operation::NONE, 0, 2));
            assert_eq!(result.status, status::TERMINAL);
            assert_eq!(result.state, state);
        }
    }
}

#[test]
fn invalid_configuration_and_untrusted_numeric_tags_are_rejected() {
    assert_eq!(agent_kernel_new(0, 100).status, status::INVALID_CONFIG);
    assert_eq!(agent_kernel_new(1, 0).status, status::INVALID_CONFIG);
    let state = initial(3);
    for (request, expected) in [
        (event(900, operation::NONE, 0, 1), status::INVALID_EVENT),
        (event(event::DISPATCH, 900, 1, 1), status::INVALID_KIND),
        (
            event(event::DISPATCH, operation::TOOL, 0, 1),
            status::INVALID_ID,
        ),
        (
            event(event::FINISH, operation::TOOL, 0, 1),
            status::INVALID_KIND,
        ),
    ] {
        let result = agent_kernel_apply(state, request);
        assert_eq!(result.status, expected);
        assert_eq!(result.state, state);
    }
    let mut invalid = state;
    invalid.abi_version += 1;
    let result = agent_kernel_apply(invalid, event(event::CANCEL, operation::NONE, 0, 1));
    assert_eq!(result.status, status::INVALID_STATE);
    assert_eq!(result.state, invalid);
}

#[test]
fn exhausted_u64_counter_does_not_overflow_or_admit_dispatch() {
    let mut state = initial(u64::MAX);
    state.operations_started = u64::MAX;
    let result = agent_kernel_apply(state, event(event::DISPATCH, operation::HOST, 1, 1));
    assert_eq!(result.status, status::OPERATION_LIMIT);
    assert_eq!(result.state.operations_started, u64::MAX);
}

#[test]
fn independent_snapshots_cannot_share_pending_work() {
    let first = applied(initial(3), event(event::DISPATCH, operation::TOOL, 1, 1));
    let second = initial(3);
    assert_eq!(first.phase, phase::WAITING);
    assert_eq!(second.phase, phase::READY);
    assert_eq!(
        agent_kernel_apply(second, event(event::SETTLE, operation::TOOL, 1, 2)).status,
        status::NO_PENDING
    );
}
