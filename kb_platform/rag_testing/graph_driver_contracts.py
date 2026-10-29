"""Offline native graph contracts: domain-free routing and cleanup, no provider calls."""
from threading import Event
import unittest

from kb_platform.agent_engine import Runtime
from kb_platform.agent_native import KernelError
from kb_platform.harness_graph import GraphProgram
from kb_platform.plugin_native import PluginError, native_call


class GraphDriverContracts(unittest.TestCase):
    def program(self, operation=None, route=None):
        def increment(flow):
            return {'count': flow['count'] + 1}

        return GraphProgram(
            operations={'increment': operation or increment,
                        'result': lambda flow: {'response': {'value': flow['count']}}},
            edges={'increment': {'again': 'increment', 'done': 'result'}, 'result': {'done': None}},
            start='increment', flow={'count': 0},
            route=route or (lambda node, flow: 'again' if node == 'increment' and flow['count'] < 3 else 'done'),
        )

    def assert_closed(self, program):
        with self.assertRaises(PluginError) as caught:
            native_call('graph_snapshot', graph_id=program.graph_id)
        self.assertEqual(caught.exception.code, 'GRAPH_NOT_FOUND')

    def test_real_native_graph_runs_domain_free_cycle_and_releases_handle(self):
        program = self.program()
        nodes = []
        result = Runtime().run(program, before_operation=nodes.append)
        self.assertEqual(result, {'value': 3})
        self.assertEqual(nodes, ['increment', 'increment', 'increment', 'result'])
        self.assert_closed(program)

    def test_callback_failure_never_retries_and_closes_graph(self):
        calls = []

        def fail(flow):
            calls.append(1)
            raise ConnectionError('synthetic failed effect')

        program = self.program(operation=fail)
        with self.assertRaisesRegex(ConnectionError, 'synthetic failed effect'):
            Runtime().run(program)
        self.assertEqual(calls, [1])
        self.assert_closed(program)

    def test_unknown_route_stops_before_next_callback(self):
        program = self.program(route=lambda node, flow: 'undeclared')
        with self.assertRaises(PluginError) as caught:
            Runtime().run(program)
        self.assertEqual(caught.exception.code, 'GRAPH_ROUTE_NOT_FOUND')
        self.assertEqual(program.flow['count'], 1)
        self.assert_closed(program)

    def test_cancellation_after_effect_does_not_accept_or_run_next_node(self):
        cancelled = Event()

        def cancel(flow):
            cancelled.set()
            return {'count': 99}

        program = self.program(operation=cancel)
        with self.assertRaises(KernelError) as caught:
            Runtime().run(program, cancellation=cancelled)
        self.assertEqual(caught.exception.code, 'AGENT_CANCELLED')
        self.assertEqual(program.flow['count'], 0)
        self.assert_closed(program)

    def test_operation_limit_closes_cyclic_graph(self):
        program = self.program(route=lambda node, flow: 'again')
        with self.assertRaises(KernelError):
            Runtime().run(program, max_operations=2)
        self.assertEqual(program.flow['count'], 2)
        self.assert_closed(program)

    def test_duplicate_execution_is_rejected_without_repeating_callback(self):
        program = self.program()
        operation = program.next()
        self.assertEqual(program.execute(operation), {'count': 1})
        with self.assertRaisesRegex(ValueError, 'GRAPH_OPERATION_OUT_OF_ORDER'):
            program.execute(operation)
        self.assert_closed(program)

    def test_unstarted_program_cancellation_never_allocates_native_graph(self):
        program = self.program()
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(KernelError):
            Runtime().run(program, cancellation=cancelled)
        self.assertIsNone(program.graph_id)


if __name__ == '__main__':
    unittest.main()
