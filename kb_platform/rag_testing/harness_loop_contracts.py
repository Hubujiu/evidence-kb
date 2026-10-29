"""Synthetic effects through the real Rust loop; no network or model credentials."""
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import unittest

from kb_platform.agent_decisions import Answer, ToolCall
from kb_platform.agent_native import KernelError
from kb_platform.agent_plugins import ToolContext, ToolPlugin, ToolPlugins, ToolResult
from kb_platform.agent_tool_loop import ToolAgent
from kb_platform.harness_services import builtin_plugins, model_tools_profile
from kb_platform.harness_sessions import MemorySession
from kb_platform.plugin_host import EventName, PluginHost, PluginSpec


CONTEXT = ToolContext('actor', 'session', 'turn')


def tool(execute, *, direct=False):
    return ToolPlugin('echo', 'Return input.', {'type': 'object', 'properties': {
        'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False},
        execute, direct_return=direct)


def replacement(profile, service, implementation, value):
    next(row for row in profile['plugins'] if row['id'] == service)['use'] = implementation
    return PluginSpec(implementation, (service,), (), lambda ctx, config: ctx.provide(service, value))


class HarnessLoopContracts(unittest.TestCase):
    def test_replaceable_context_and_session_own_exact_model_history(self):
        profile, records, requests = model_tools_profile(), [], []

        class Session(MemorySession):
            def append(self, rows):
                records.extend(deepcopy(rows))
                super().append(rows)

            def finish(self, value):
                records.append({'committed': value})
                super().finish(value)

        class Prompt:
            def prepare(self, messages, tools, context):
                return {'messages': messages, 'tools': tools, 'prepared_by': 'fixture'}

        class Model:
            def decide(self, request, context):
                requests.append(deepcopy(request))
                return ({'kind': 'answer', 'value': request['messages'][-1]['content']}
                        if request['messages'][-1]['role'] == 'tool'
                        else {'kind': 'tool', 'name': 'echo', 'arguments': {'text': 'hello'}})

        sessions = SimpleNamespace(begin=lambda context, message: Session(message))
        replacements = [replacement(profile, 'sessions', 'fixture.sessions', sessions),
                        replacement(profile, 'prompt', 'fixture.prompt', Prompt()),
                        replacement(profile, 'model', 'fixture.model', Model())]
        catalog = (*builtin_plugins(tools=(tool(lambda ctx, args: ToolResult(args)),)), *replacements)
        with PluginHost(profile, catalog) as host:
            result = host.call('loop', 'run', 'input', CONTEXT)
        self.assertEqual(result, {'text': 'hello'})
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]['prepared_by'], 'fixture')
        self.assertEqual(requests[1]['messages'], [
            {'role': 'user', 'content': 'input'},
            {'role': 'assistant', 'tool_call': {'name': 'echo', 'arguments': {'text': 'hello'}}},
            {'role': 'tool', 'name': 'echo', 'content': {'text': 'hello'}}])
        self.assertEqual(records[-2:], [{'role': 'assistant', 'content': result}, {'committed': result}])

    def test_session_cached_null_prevents_any_new_model_effect(self):
        profile = model_tools_profile()
        session = MemorySession('cached')
        session.start_result = {'cached': None}
        selected = replacement(profile, 'sessions', 'fixture.sessions',
            SimpleNamespace(begin=lambda context, message: session))
        def forbidden(*args):
            self.fail('A cached session must not invoke the model')
        agent = ToolAgent(forbidden, profile=profile, extra_plugins=(selected,))
        self.assertIsNone(agent.run('cached', CONTEXT))

    def test_failure_never_retries_tool_and_fails_session_without_commit(self):
        profile, outcomes, calls = model_tools_profile(), [], []
        class Session(MemorySession):
            def finish(self, result):
                outcomes.append('committed')
            def fail(self, error):
                outcomes.append(str(error))
                super().fail(error)
        selected = replacement(profile, 'sessions', 'fixture.sessions',
            SimpleNamespace(begin=lambda context, message: Session(message)))
        def effect(ctx, args):
            calls.append(ctx.call_id)
            raise RuntimeError('ambiguous physical request')
        agent = ToolAgent(lambda *args: ToolCall('echo', {'text': 'x'}), (tool(effect),),
                          profile=profile, extra_plugins=(selected,))
        with self.assertRaisesRegex(RuntimeError, 'ambiguous physical request'):
            agent.run('input', CONTEXT)
        self.assertEqual(calls, ['turn:1'])
        self.assertEqual(outcomes, ['ambiguous physical request'])

    def test_cancel_from_observer_stops_before_tool_dispatch(self):
        cancellation, calls = Event(), []
        observer = PluginSpec('fixture.observe', (), (),
            lambda ctx, config: ctx.on(EventName.TOOL_BEFORE, lambda event: cancellation.set()))
        profile = model_tools_profile()
        profile['plugins'].append({'id': 'observe', 'use': observer.key})
        agent = ToolAgent(lambda *args: ToolCall('echo', {'text': 'x'}),
            (tool(lambda ctx, args: calls.append(args) or ToolResult(args)),),
            profile=profile, extra_plugins=(observer,))
        with self.assertRaisesRegex(KernelError, 'AGENT_CANCELLED'):
            agent.run('input', CONTEXT, cancellation=cancellation)
        self.assertEqual(calls, [])

    def test_bounded_native_loop_does_not_repeat_model_forever(self):
        calls = []
        agent = ToolAgent(lambda *args: calls.append('model') or ToolCall('echo', {'text': 'x'}),
                          (tool(lambda ctx, args: ToolResult(args)),))
        with self.assertRaises(KernelError):
            agent.run('input', CONTEXT, max_operations=7)
        self.assertEqual(calls, ['model'])

    def test_fresh_run_does_not_reuse_other_session_evidence(self):
        captured = []
        agent = ToolAgent(lambda messages, tools: captured.append(messages) or Answer('ok'))
        agent.run('private-one', CONTEXT)
        agent.run('private-two', ToolContext('other', 'other-session', 'other-turn'))
        self.assertEqual(captured, [[{'role': 'user', 'content': 'private-one'}],
                                    [{'role': 'user', 'content': 'private-two'}]])

    def test_single_tool_uses_selected_binding_provider_and_preserves_trusted_call_id(self):
        profile = model_tools_profile()
        profile['plugins'] = [row for row in profile['plugins'] if row['id'] not in {'model', 'prompt'}]
        next(row for row in profile['plugins'] if row['id'] == 'loop')['use'] = 'harness.loop'
        calls = []
        class Tools(ToolPlugins):
            def bind(self, plugins):
                calls.append('bind')
                return super().bind(plugins)
        selected = replacement(profile, 'tools', 'fixture.tools', Tools())
        with PluginHost(profile, (*builtin_plugins(), selected)) as host:
            result = host.call('loop', 'invoke',
                tool(lambda ctx, args: calls.append(ctx.call_id) or ToolResult(args)),
                ToolContext('actor', 'session', 'turn', call_id='trusted-http-id'), {'text': 'x'})
            self.assertEqual(host.service('tools').names(), [])
        self.assertEqual(result, {'text': 'x'})
        self.assertEqual(calls, ['bind', 'trusted-http-id'])


if __name__ == '__main__':
    unittest.main()
