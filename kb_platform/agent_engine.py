"""Generic native lifecycle with host-owned effects and explicit cooperative cancellation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
from threading import Event
import time
from typing import Protocol

from .agent_native import Kernel, KernelError, PROTOCOL


@dataclass(frozen=True)
class Operation:
    name: str
    kind: str = 'host'
    arguments: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or self.kind not in {'host', 'model', 'tool'}:
            raise ValueError('INVALID_OPERATION')
        if not isinstance(self.arguments, dict):
            raise ValueError('INVALID_OPERATION_ARGUMENTS')


@dataclass(frozen=True)
class Complete:
    value: object


class Program(Protocol):
    def next(self) -> Operation | Complete: ...
    def execute(self, operation: Operation) -> object: ...
    def accept(self, operation: Operation, result: object) -> None: ...


@dataclass(frozen=True)
class Execution:
    deadline: float
    cancellation: Event

    @property
    def remaining_seconds(self):
        return max(0.0, self.deadline - time.monotonic())

    def check(self):
        if self.cancellation.is_set():
            raise KernelError('AGENT_CANCELLED')
        if time.monotonic() >= self.deadline:
            raise KernelError('AGENT_DEADLINE_EXCEEDED')


_execution = ContextVar('agent_execution', default=None)


def current_execution() -> Execution | None:
    return _execution.get()


@contextmanager
def execution_scope(*, deadline_seconds, cancellation=None):
    if (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
        raise ValueError('INVALID_RUNTIME_DEADLINE')
    parent = current_execution()
    deadline = time.monotonic() + deadline_seconds
    if parent is not None:
        parent.check()
        deadline = min(deadline, parent.deadline)
        # A child cannot detach itself from its parent's cancellation authority.
        cancellation = parent.cancellation
    execution = Execution(deadline, cancellation if cancellation is not None else Event())
    token = _execution.set(execution)
    try:
        execution.check()
        yield execution
    finally:
        _execution.reset(token)


class Runtime:
    protocol = PROTOCOL

    def run(self, program: Program, *, before_operation=None, max_operations=128,
            deadline_seconds=900, cancellation=None):
        with execution_scope(deadline_seconds=deadline_seconds, cancellation=cancellation) as execution:
            started = time.monotonic()
            kernel = Kernel(max_operations, max(1, math.ceil(execution.remaining_seconds * 1000)))

            def apply(event, operation_id=0, operation_kind='none'):
                kernel.apply(event, operation_id=operation_id, operation_kind=operation_kind,
                             elapsed_ms=max(0, int((time.monotonic() - started) * 1000)))

            try:
                while True:
                    execution.check()
                    instruction = program.next()
                    execution.check()
                    if isinstance(instruction, Complete):
                        apply('finish')
                        return instruction.value
                    if not isinstance(instruction, Operation):
                        raise TypeError('INVALID_PROGRAM_INSTRUCTION')
                    if before_operation is not None:
                        before_operation(instruction.name)
                    execution.check()
                    operation_id = kernel.operations_started + 1
                    apply('dispatch', operation_id, instruction.kind)
                    result = program.execute(instruction)
                    execution.check()
                    apply('settle', operation_id, instruction.kind)
                    program.accept(instruction, result)
            except BaseException:
                if kernel.phase in {'ready', 'waiting'}:
                    # Cleanup never retries an effect and cannot replace the original error.
                    try:
                        apply('cancel' if execution.cancellation.is_set() else 'fail')
                    except KernelError:
                        pass
                raise
