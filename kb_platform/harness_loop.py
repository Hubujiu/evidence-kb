"""Execute native loop effects through injected services, without domain decisions."""
from copy import deepcopy
from dataclasses import replace

from .agent_engine import Complete, Operation, current_execution, execution_scope
from .plugin_host import EventName
from .plugin_native import native_call


class NativeLoop:
    def __init__(self, host, runtime, sessions, prompt, tools, *, model=None, mode='model-tools'):
        self.host, self.runtime, self.sessions = host, runtime, sessions
        self.prompt, self.tools, self.model, self.mode = prompt, tools, model, mode

    def run(self, message, context, *, max_operations=128, deadline_seconds=900,
            cancellation=None, before_operation=None):
        if self.mode != 'model-tools':
            raise ValueError('LOOP_REQUIRES_EXPLICIT_TOOL')
        return self._run(message, context, self.tools, {}, max_operations=max_operations,
            deadline_seconds=deadline_seconds, cancellation=cancellation, before_operation=before_operation)

    def invoke(self, tool, context, arguments, *, max_operations=128, deadline_seconds=900,
               cancellation=None, before_operation=None):
        if self.mode != 'single-tool':
            raise ValueError('LOOP_REQUIRES_MODEL_INPUT')
        bound = self.tools.bind((tool,))
        return self._run(arguments, context, bound, {'tool_name': tool.name, 'arguments': arguments},
            max_operations=max_operations, deadline_seconds=deadline_seconds,
            cancellation=cancellation, before_operation=before_operation)

    def _run(self, message, context, tools, config, **limits):
        with self.host.lease(), execution_scope(deadline_seconds=limits['deadline_seconds'],
                                              cancellation=limits['cancellation']) as execution:
            program = _NativeProgram(self, tools, context, message, config)
            try:
                self.host.emit(EventName.RUN_STARTED, program.identity)
                result = self.runtime.run(program, **limits)
                execution.check()
                if program.session is not None:
                    program.session.finish(result)
                self.host.emit(EventName.RUN_COMPLETED, program.identity)
                return result
            except BaseException as error:
                if program.session is not None:
                    try:
                        program.session.fail(error)
                    except BaseException as cleanup:
                        error.add_note(f'Session failure cleanup: {cleanup}')
                self.host.emit(EventName.RUN_FAILED, {**program.identity, 'error': type(error).__name__})
                raise
            finally:
                native_call('loop_close', loop_id=program.id)


class _NativeProgram:
    def __init__(self, loop, tools, context, message, config):
        self.loop, self.tools, self.context = loop, tools, context
        self.session, self.effect_id, self.tool_count = None, None, 0
        self.identity = {'session_id': context.session_id, 'turn_id': context.turn_id}
        self.id = native_call('loop_create', mode=loop.mode, message=message,
                              tools=tools.manifest(), **config)['loop_id']

    def next(self):
        instruction = native_call('loop_next', loop_id=self.id)
        if instruction['kind'] == 'complete':
            return Complete(instruction['value'])
        self.effect_id = instruction['effect_id']
        return Operation(instruction['name'], instruction['kind'], instruction['arguments'])

    def execute(self, operation):
        if operation.kind == 'host':
            return self._host_effect(operation)
        before, after = ((EventName.MODEL_BEFORE, EventName.MODEL_AFTER) if operation.kind == 'model'
                         else (EventName.TOOL_BEFORE, EventName.TOOL_AFTER))
        metadata = {**self.identity, 'name': operation.name, 'effect_id': self.effect_id}
        self.loop.host.emit(before, metadata)
        current_execution().check()
        if operation.kind == 'model':
            result = self.loop.model.decide(deepcopy(operation.arguments), self.context)
        else:
            call_id = (self.context.call_id if self.loop.mode == 'single-tool' and self.context.call_id
                       else f'{self.context.turn_id}:{self.tool_count + 1}')
            context = replace(self.context, call_id=call_id)
            result = self.tools.execute(operation.name, context, operation.arguments).data
        self.loop.host.emit(after, metadata)
        return result

    def _host_effect(self, operation):
        if operation.name == 'session.start':
            self.session = self.loop.sessions.begin(self.context, operation.arguments['message'])
            return deepcopy(self.session.start_result)
        if operation.name == 'context.prepare':
            return self.loop.prompt.prepare(self.session.messages(), self.tools.manifest(), self.context)
        if operation.name == 'session.append':
            self.session.append(operation.arguments['records'])
            return {}
        raise ValueError('UNKNOWN_NATIVE_HOST_EFFECT')

    def accept(self, operation, result):
        state = native_call('loop_accept', loop_id=self.id, effect_id=self.effect_id, result=result)
        self.tool_count = state['tool_count']
        self.effect_id = None
