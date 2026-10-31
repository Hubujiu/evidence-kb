from copy import deepcopy

from .agent_engine import Complete, Operation
from .plugin_native import native_call


class GraphProgram:


    def __init__(self, *, operations, edges, start, route, flow=None, result_key='response', kinds=None):
        if (not isinstance(operations, dict) or not operations or not isinstance(edges, dict)
                or set(operations) != set(edges) or not all(callable(value) for value in operations.values())
                or not callable(route) or flow is not None and not isinstance(flow, dict)
                or not isinstance(result_key, str) or not result_key):
            raise ValueError('INVALID_GRAPH_PROGRAM')
        kinds = {} if kinds is None else kinds
        if not isinstance(kinds, dict) or set(kinds) - set(operations):
            raise ValueError('INVALID_GRAPH_KINDS')
        self.operations, self.flow = dict(operations), {} if flow is None else flow
        self._config = {'start': start, 'nodes': {
            name: {'kind': kinds.get(name, 'host'), 'routes': deepcopy(edges[name])}
            for name in operations}}
        self._route, self._result_key = route, result_key
        self.stage, self.graph_id = start, None
        self._operation, self._effect_id = None, None
        self._executed = self._closed = False

    def next(self):
        if self._closed:
            raise ValueError('GRAPH_PROGRAM_CLOSED')
        try:
            if self.graph_id is None:
                self.graph_id = native_call('graph_create', **self._config)['graph_id']
            instruction = native_call('graph_next', graph_id=self.graph_id)
            if instruction['kind'] == 'complete':
                return Complete(self.flow[self._result_key])
            self.stage = instruction['name']
            self._effect_id = instruction['effect_id']
            self._operation = Operation(self.stage, instruction['kind'])
            self._executed = False
            return self._operation
        except BaseException as error:
            self._close_after_error(error)
            raise

    def execute(self, operation):
        try:
            if self._closed or operation is not self._operation or self._executed:
                raise ValueError('GRAPH_OPERATION_OUT_OF_ORDER')
            self._executed = True
            result = self.operations[operation.name](self.flow)
            if not isinstance(result, dict):
                raise ValueError('INVALID_GRAPH_OPERATION_RESULT')
            return result
        except BaseException as error:
            self._close_after_error(error)
            raise

    def accept(self, operation, result):
        try:
            if (self._closed or operation is not self._operation or not self._executed
                    or not isinstance(result, dict)):
                raise ValueError('INVALID_GRAPH_OPERATION_RESULT')
            self.flow.update(result)
            route = self._route(operation.name, self.flow)
            if not isinstance(route, str):
                raise ValueError('INVALID_GRAPH_ROUTE')
            snapshot = native_call('graph_accept', graph_id=self.graph_id,
                                   effect_id=self._effect_id, route=route)
            self.stage = snapshot['node']
            self._operation = self._effect_id = None
        except BaseException as error:
            self._close_after_error(error)
            raise

    def close(self):
        if self._closed:
            return
        if self.graph_id is not None:
            native_call('graph_close', graph_id=self.graph_id)
        self._closed = True

    def _close_after_error(self, error):
        try:
            self.close()
        except BaseException as cleanup:
            error.add_note(f'Graph cleanup failed: {cleanup}')
