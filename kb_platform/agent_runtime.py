from contextlib import contextmanager
from contextvars import ContextVar
import time

from .provider_runtime import RuntimePolicyError
from .agent_engine import current_execution


_deadline = ContextVar('agent_send_deadline', default=None)


@contextmanager
def send_deadline(deadline):
    previous = _deadline.get()
    effective = previous if deadline is None else min(previous, deadline) if previous is not None else deadline
    token = _deadline.set(effective)
    try:
        yield
    finally:
        _deadline.reset(token)


def serialized_send(send, lock, *args, **kwargs):

    deadline = _deadline.get()
    execution = current_execution()
    if execution is not None:
        if execution.cancellation.is_set():
            raise RuntimePolicyError('PROVIDER_CANCELLED')
        deadline = min(deadline, execution.deadline) if deadline is not None else execution.deadline
    started = time.monotonic()
    remaining = None if deadline is None else deadline - started
    if remaining is not None and remaining <= 0:
        raise RuntimePolicyError('PROVIDER_DEADLINE_EXCEEDED')
    acquired = lock.acquire() if remaining is None else lock.acquire(timeout=remaining)
    if not acquired:
        raise RuntimePolicyError('PROVIDER_DEADLINE_EXCEEDED')
    try:
        if execution is not None and execution.cancellation.is_set():
            raise RuntimePolicyError('PROVIDER_CANCELLED')
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise RuntimePolicyError('PROVIDER_DEADLINE_EXCEEDED')
        if args and remaining is not None:
            request = args[0]
            timeout = request.extensions.get('timeout', {})
            request.extensions['timeout'] = {
                key: min(value, remaining) if value is not None else remaining
                for key, value in timeout.items()}
            request.extensions['agent_queue_seconds'] = time.monotonic() - started
        return send(*args, **kwargs)
    finally:
        lock.release()
