"""Synthetic native/host integration contracts; real Rust transitions, no provider traffic."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import httpx

from kb_platform.agent_engine import Complete, Operation, Runtime, current_execution, execution_scope
from kb_platform.agent_native import Kernel, KernelError
from kb_platform.agent_plugins import ToolContext, ToolPlugin, ToolPlugins, ToolResult
from kb_platform.agent_runtime import serialized_send
from kb_platform.agent_tool_loop import Answer, ToolAgent, ToolCall
from kb_platform.knowledge_tool import query_tool
from kb_platform.knowledge_answer import KnowledgeAnswer
from kb_platform.provider_runtime import RuntimePolicyError
from . import agent_graph_contracts as fixtures


class CounterProgram:
    def __init__(self, count=1, execute=None):
        self.count, self.calls, self.accepted = count, [], []
        self.effect = execute or (lambda: 42)

    def next(self):
        return Operation('ordinary_tool', 'tool') if len(self.accepted) < self.count else Complete(self.accepted)

    def execute(self, operation):
        self.calls.append(operation.name)
        return self.effect()

    def accept(self, operation, result):
        self.accepted.append(result)


class NativeRuntimeContracts(unittest.TestCase):
    def test_native_result_identity_and_terminal_states_reject_replay(self):
        kernel = Kernel(2, 100)
        kernel.apply('dispatch', operation_id=1, operation_kind='tool', elapsed_ms=1)
        with self.assertRaises(KernelError):
            kernel.apply('settle', operation_id=2, operation_kind='tool', elapsed_ms=2)
        self.assertEqual(kernel.phase, 'waiting')
        kernel.apply('settle', operation_id=1, operation_kind='tool', elapsed_ms=3)
        with self.assertRaises(KernelError):
            kernel.apply('settle', operation_id=1, operation_kind='tool', elapsed_ms=4)
        kernel.apply('finish', elapsed_ms=5)
        with self.assertRaises(KernelError):
            kernel.apply('dispatch', operation_id=2, operation_kind='tool', elapsed_ms=6)
        self.assertEqual(kernel.phase, 'completed')

    def test_native_expiry_and_cancellation_reject_late_success(self):
        for terminal in ('expiry', 'cancel'):
            with self.subTest(terminal=terminal):
                kernel = Kernel(2, 10)
                kernel.apply('dispatch', operation_id=1, operation_kind='model', elapsed_ms=1)
                if terminal == 'cancel':
                    kernel.apply('cancel', elapsed_ms=2)
                with self.assertRaises(KernelError):
                    kernel.apply('settle', operation_id=1, operation_kind='model', elapsed_ms=10)
                self.assertEqual(kernel.phase, 'expired' if terminal == 'expiry' else 'cancelled')

    def test_limit_stops_before_another_effect_and_never_retries(self):
        program = CounterProgram(count=2)
        with self.assertRaises(KernelError) as failure:
            Runtime().run(program, max_operations=1)
        self.assertEqual(failure.exception.code, 'AGENT_OPERATION_LIMIT')
        self.assertEqual(program.calls, ['ordinary_tool'])
        self.assertEqual(program.accepted, [42])

    def test_cancelled_inflight_effect_is_not_accepted_or_retried(self):
        cancellation = Event()
        def effect():
            cancellation.set()
            return 'late'
        program = CounterProgram(execute=effect)
        with self.assertRaises(KernelError) as failure:
            Runtime().run(program, cancellation=cancellation)
        self.assertEqual(failure.exception.code, 'AGENT_CANCELLED')
        self.assertEqual(program.calls, ['ordinary_tool'])
        self.assertEqual(program.accepted, [])

    def test_nested_execution_cannot_extend_deadline_or_replace_cancellation(self):
        cancellation = Event()
        with execution_scope(deadline_seconds=10, cancellation=cancellation) as parent:
            with execution_scope(deadline_seconds=100, cancellation=Event()) as child:
                self.assertEqual(child.deadline, parent.deadline)
                cancellation.set()
                with self.assertRaises(KernelError):
                    child.check()
            self.assertIs(current_execution(), parent)
        self.assertIsNone(current_execution())

    def test_cancel_after_send_queue_acquisition_never_sends(self):
        cancellation = Event()
        class CancelLock:
            released = False
            def acquire(self, **kwargs):
                cancellation.set()
                return True
            def release(self):
                self.released = True
        lock, sent = CancelLock(), []
        with execution_scope(deadline_seconds=10, cancellation=cancellation):
            with self.assertRaises(RuntimePolicyError):
                serialized_send(lambda request: sent.append(request), lock,
                                httpx.Request('GET', 'https://example.invalid'))
        self.assertTrue(lock.released)
        self.assertEqual(sent, [])

    def test_missing_library_does_not_silently_run_python(self):
        with TemporaryDirectory() as directory:
            with patch.dict('os.environ', {'KB_AGENT_KERNEL_LIBRARY': str(Path(directory) / 'missing.dll')}):
                program = CounterProgram()
                with self.assertRaises(KernelError) as failure:
                    Runtime().run(program)
        self.assertEqual(failure.exception.code, 'AGENT_KERNEL_UNAVAILABLE')
        self.assertEqual(program.calls, [])

    def test_plain_tool_agent_works_without_knowledge_capabilities(self):
        calls = []
        def execute(context, arguments):
            calls.append(context.call_id)
            return ToolResult({'value': arguments['value'] * 2})
        plugin = ToolPlugin('double', 'Double an integer.', {'type': 'object',
            'properties': {'value': {'type': 'integer'}}, 'required': ['value'],
            'additionalProperties': False}, execute)
        def decide(messages, manifest):
            self.assertEqual([item['name'] for item in manifest], ['double'])
            return Answer(messages[-1]['content']['value']) if messages[-1]['role'] == 'tool' else ToolCall('double', {'value': 21})
        agent = ToolAgent(decide, [plugin])
        self.assertEqual(agent.run('double', ToolContext('actor', 'session', 'turn')), 42)
        self.assertEqual(calls, ['turn:1'])
        self.assertEqual(ToolPlugins().names(), [])

    def test_failure_does_not_repeat_a_side_effect(self):
        def effect():
            raise RuntimeError('effect failed after possible execution')
        program = CounterProgram(execute=effect)
        with self.assertRaisesRegex(RuntimeError, 'effect failed'):
            Runtime().run(program)
        self.assertEqual(len(program.calls), 1)
        self.assertEqual(program.accepted, [])


class KnowledgeToolContracts(unittest.TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service

    def test_knowledge_tool_uses_existing_service_once_and_delivers_verified_result(self):
        case = self.setup_service(fixtures.normal_script())
        plans = []
        def decide(messages, manifest):
            plans.append(1)
            self.assertEqual([item['name'] for item in manifest], ['kb_query'])
            return ToolCall('kb_query', {'question': 'What is the original rule?'})
        agent = ToolAgent(decide, [query_tool(case.service, case.session)])
        context = ToolContext(case.authority.actor, 'outer-session', 'outer-turn')
        answer = agent.run('question', context)
        self.assertEqual(answer['status'], 'answered', answer)
        self.assertEqual(answer['answer'], 'Original rule: value 42.')
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(case.model.calls), 4)
        repeated = agent.run('question', context)
        self.assertEqual(answer, repeated)
        self.assertEqual(len(case.model.calls), 4)

    def test_model_cannot_choose_actor_or_knowledge_session(self):
        case = self.setup_service()
        tools = ToolPlugins([query_tool(case.service, case.session)])
        with self.assertRaises(ValueError):
            tools.execute('kb_query', ToolContext(case.authority.actor, 's', 't', call_id='1'),
                          {'question': 'hello', 'session_id': 'another-user-session'})
        self.assertEqual(case.model.calls, [])

    def test_bound_knowledge_sessions_have_distinct_plugin_identities(self):
        case = self.setup_service()
        first = ToolPlugins([query_tool(case.service, case.session)]).manifest()
        second = ToolPlugins([query_tool(case.service, 'other-session')]).manifest()
        self.assertNotEqual(first, second)

    def test_cancelled_delivery_publishes_neither_assistant_history_nor_last_answer(self):
        case = self.setup_service(fixtures.normal_script())
        cancellation = Event()
        original = KnowledgeAnswer.deliver_node
        def cancelled_delivery(turn, flow):
            result = original(turn, flow)
            cancellation.set()
            return result
        with execution_scope(deadline_seconds=30, cancellation=cancellation), \
                patch.object(KnowledgeAnswer, 'deliver_node', cancelled_delivery):
            result = case.service.turn(case.authority.actor, case.session, message='original rule',
                                       request_id='cancelled', expected_revision=0)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'AGENT_CANCELLED')
        events = case.service.store.events(case.authority.actor, case.session)
        self.assertEqual([event for event in events if event['kind'] == 'assistant'], [])
        self.assertIsNone(case.service.store.load(case.authority.actor, case.session)['state']['last_answer'])
