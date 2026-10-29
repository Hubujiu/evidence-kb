"""Offline core-bundle integration: real native loop and SQLite, synthetic effects."""
from copy import deepcopy
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from kb_platform.agent_decisions import Answer, ToolCall
from kb_platform.agent_plugins import ToolPlugin, ToolResult
from kb_platform.harness_profile import create_agent_application, load_agent_profile
from kb_platform.plugin_host import PluginSpec
from kb_platform.session_identity import Actor, PlatformError, SessionPersistenceError


OWNER = Actor('tenant', 'owner')


def echo(effect):
    return ToolPlugin('echo', 'Return the supplied text.', {'type': 'object', 'properties': {
        'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False}, effect)


class GenericAgentContracts(unittest.TestCase):
    def application(self, directory, decide, *, tools=(), profile=None, extra_plugins=()):
        profile = profile or load_agent_profile(environment={})
        next(row for row in profile['plugins'] if row['id'] == 'store')['config']['root'] = directory
        application, host = create_agent_application(decide=decide, tools=tools, profile=profile,
                                                     extra_plugins=extra_plugins, environment={})
        self.addCleanup(host.close)
        return application, host

    @staticmethod
    def turn(app, session, message, request_id, revision, **limits):
        return app.turn(OWNER, session, message=message, request_id=request_id,
                        expected_revision=revision, **limits)

    def test_core_bundle_restores_paired_tool_history_across_hosts_and_replays_once(self):
        requests, effects = [], []
        def decide(messages, tools):
            requests.append(deepcopy(messages))
            return (ToolCall('echo', {'text': 'receipt'}) if messages[-1]['content'] == 'first'
                    else Answer('answer'))
        with TemporaryDirectory() as directory:
            app, host = self.application(directory, decide, tools=(echo(
                lambda context, args: effects.append(context.call_id) or ToolResult(args)),))
            session = app.create(OWNER)['session_id']
            first = self.turn(app, session, 'first', 'one', 0)
            self.assertEqual(first['result'], 'answer')
            self.assertEqual(first['revision'], 1)
            self.assertEqual(effects, ['one:1'])
            host.close()
            resumed, _ = self.application(directory, decide)
            self.assertEqual(self.turn(resumed, session, 'first', 'one', 0), first)
            self.assertEqual(len(requests), 2)
            second = self.turn(resumed, session, 'continue', 'two', 1)
            self.assertEqual(second['revision'], 2)
            self.assertEqual(requests[-1], [
                {'role': 'user', 'content': 'first'},
                {'role': 'assistant', 'tool_call': {'name': 'echo', 'arguments': {'text': 'receipt'}}},
                {'role': 'tool', 'name': 'echo', 'content': {'text': 'receipt'}},
                {'role': 'assistant', 'content': 'answer'},
                {'role': 'user', 'content': 'continue'}])
            self.assertEqual(resumed.get(OWNER, session)['state']['turn_number'], 2)

    def test_failed_model_preserves_successful_tool_receipt_for_next_turn(self):
        requests, effects = [], []
        def decide(messages, tools):
            requests.append(deepcopy(messages))
            if messages[-1]['role'] == 'tool':
                raise RuntimeError('synthetic model failure')
            if messages[-1]['content'] == 'perform operation':
                return ToolCall('echo', {'text': 'performed'})
            return Answer('previous operation was already performed')
        with TemporaryDirectory() as directory:
            app, host = self.application(directory, decide, tools=(echo(
                lambda context, args: effects.append(context.call_id) or ToolResult(args)),))
            session = app.create(OWNER)['session_id']
            failed = self.turn(app, session, 'perform operation', 'one', 0)
            self.assertEqual(failed['status'], 'error')
            self.assertEqual(self.turn(app, session, 'perform operation', 'one', 0), failed)
            self.assertEqual(len(requests), 2)
            host.close()
            resumed, _ = self.application(directory, decide)
            response = self.turn(resumed, session, 'continue', 'two', failed['revision'])
            self.assertEqual(response['status'], 'complete')
            self.assertEqual(effects, ['one:1'])
            history = requests[-1]
            self.assertEqual(history[0], {'role': 'user', 'content': 'perform operation'})
            self.assertEqual(history[2], {'role': 'tool', 'name': 'echo', 'content': {'text': 'performed'}})
            self.assertEqual(history[3]['source'], 'runtime')
            self.assertEqual(history[3]['content']['runtime_status'], 'failed')

    def test_ambiguous_tool_outcome_blocks_new_turn_without_mutation_or_retry(self):
        effects, models = [], []
        def effect(context, args):
            effects.append(context.call_id)
            raise RuntimeError('unknown physical outcome')
        with TemporaryDirectory() as directory:
            app, host = self.application(directory,
                lambda *args: models.append(1) or ToolCall('echo', {'text': 'write'}), tools=(echo(effect),))
            session = app.create(OWNER)['session_id']
            failed = self.turn(app, session, 'perform operation', 'one', 0)
            self.assertEqual(failed['status'], 'error')
            host.close()
            resumed, _ = self.application(directory, lambda *args: models.append(1) or Answer('unexpected'))
            self.assertEqual(self.turn(resumed, session, 'perform operation', 'one', 0), failed)
            before = resumed.history(OWNER, session)
            with self.assertRaises(PlatformError) as raised:
                self.turn(resumed, session, 'continue', 'two', failed['revision'])
            self.assertEqual(raised.exception.code, 'SESSION_OUTCOME_UNKNOWN')
            self.assertEqual(resumed.history(OWNER, session), before)
            self.assertEqual(effects, ['one:1'])
            self.assertEqual(models, [1])

    def test_cancellation_cannot_publish_terminal_answer_and_replay_is_stable(self):
        cancellation, calls = Event(), []
        def decide(*args):
            calls.append(1)
            cancellation.set()
            return Answer('must never be committed')
        with TemporaryDirectory() as directory:
            app, _ = self.application(directory, decide)
            session = app.create(OWNER)['session_id']
            failed = self.turn(app, session, 'question', 'one', 0, cancellation=cancellation)
            self.assertEqual(failed['status'], 'error')
            self.assertEqual(failed['stop_reason'], 'AGENT_CANCELLED')
            self.assertFalse(any(event['kind'] == 'assistant' for event in app.history(OWNER, session)['events']))
            self.assertEqual(self.turn(app, session, 'question', 'one', 0), failed)
            self.assertEqual(calls, [1])

    def test_owner_namespace_revision_and_request_identity_precede_model_calls(self):
        calls = []
        with TemporaryDirectory() as directory:
            app, host = self.application(directory, lambda *args: calls.append(1) or Answer('ok'))
            session = app.create(OWNER)['session_id']
            with self.assertRaises(PlatformError):
                app.get(Actor('tenant', 'other'), session)
            foreign = host.service('session.store').create(OWNER, 'scope', {'namespace': 'knowledge/1'})
            with self.assertRaises(PlatformError) as raised:
                self.turn(app, foreign['session_id'], 'read', 'foreign', 0)
            self.assertEqual(raised.exception.code, 'SESSION_KIND_MISMATCH')
            with self.assertRaises(PlatformError) as raised:
                self.turn(app, session, 'read', 'one', 9)
            self.assertEqual(raised.exception.code, 'SESSION_REVISION_CONFLICT')
            self.assertEqual(calls, [])
            self.turn(app, session, 'read', 'one', 0)
            with self.assertRaises(PlatformError) as raised:
                self.turn(app, session, 'different', 'one', 0)
            self.assertEqual(raised.exception.code, 'REQUEST_ID_REUSED')
            self.assertEqual(calls, [1])

    def test_invalid_request_id_is_rejected_before_persistent_admission(self):
        calls = []
        with TemporaryDirectory() as directory:
            app, _ = self.application(directory, lambda *args: calls.append(1) or Answer('ok'))
            session = app.create(OWNER)['session_id']
            with self.assertRaisesRegex(ValueError, 'INVALID_AGENT_REQUEST_ID'):
                self.turn(app, session, 'question', 'x' * 161, 0)
            self.assertEqual(app.get(OWNER, session)['revision'], 0)
            self.assertEqual(app.history(OWNER, session)['events'], [])
            self.assertEqual(calls, [])

    def test_commit_failure_raises_and_never_publishes_success(self):
        with TemporaryDirectory() as directory:
            app, host = self.application(directory, lambda *args: Answer('candidate'))
            session = app.create(OWNER)['session_id']
            with patch.object(host.service('session.store'), 'finish', side_effect=OSError('disk failure')):
                with self.assertRaises(SessionPersistenceError):
                    self.turn(app, session, 'question', 'one', 0)
            events = app.history(OWNER, session)['events']
            self.assertFalse(any(event['kind'] == 'assistant' for event in events))
            self.assertTrue(all(event['payload']['status'] == 'error'
                                for event in events if event['kind'] == 'turn_completed'))

    def test_application_accepts_replacement_driver_through_public_run_contract(self):
        from kb_platform.harness_loop import NativeLoop

        calls = []
        def mount(ctx, config):
            delegate = NativeLoop(ctx.host, ctx.require('runtime'), ctx.require('sessions'),
                                  ctx.require('prompt'), ctx.require('tools'),
                                  model=ctx.require('model'), mode='model-tools')
            class Driver:
                def run(self, message, context, **limits):
                    calls.append(context.session_id)
                    return delegate.run(message, context, **limits)
            ctx.provide('loop', Driver())
        spec = PluginSpec('fixture.driver', ('loop',), ('runtime', 'sessions', 'prompt', 'tools', 'model'), mount)
        profile = load_agent_profile(environment={})
        next(row for row in profile['plugins'] if row['id'] == 'loop').update(use=spec.key, config={})
        with TemporaryDirectory() as directory:
            app, _ = self.application(directory, lambda *args: Answer('ok'), profile=profile,
                                       extra_plugins=(spec,))
            session = app.create(OWNER)['session_id']
            self.assertEqual(self.turn(app, session, 'question', 'one', 0)['result'], 'ok')
            self.assertEqual(calls, [session])


if __name__ == '__main__':
    unittest.main()
