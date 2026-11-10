from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import select

from kb_platform.agent_decisions import Answer, ToolCall
from kb_platform.agent_plugins import ToolPlugin, ToolResult
from kb_platform.database_contracts import CommitUnknown
from kb_platform.harness_profile import create_agent_application, load_agent_profile
from kb_platform.harness_sessions import DurableSessions, SessionToolContext
from kb_platform.session_identity import Actor, PlatformError, SessionPersistenceError
from kb_platform.session_storage.schema import artifacts, events, requests, sessions
from kb_platform.session_store import SessionStore


OWNER = Actor('synthetic-tenant', 'synthetic-owner')


@contextmanager
def lost_ack(store):
    transaction = store.database.transaction

    @contextmanager
    def commit_then_lose(*args, **kwargs):
        with transaction(*args, **kwargs) as current:
            yield current
        if kwargs.get('write', True):
            raise CommitUnknown('synthetic-durable-session-commit')

    with patch.object(store.database, 'transaction', side_effect=commit_then_lose):
        yield


def rows(store):
    with store.database.transaction(write=False) as transaction:
        return {table.name: [dict(row) for row in transaction.execute(select(table)).rows]
                for table in (sessions, requests, events, artifacts)}


class DurableSessionTransitionContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def direct(self):
        store = SessionStore(self.root / 'direct')
        self.addCleanup(store.close)
        adapter = DurableSessions(store)
        sid = adapter.create(OWNER)['session_id']
        context = SessionToolContext(OWNER, sid, 'turn', expected_revision=0)
        return store, adapter, sid, context

    def application(self, decide, effect=None):
        profile = load_agent_profile(environment={})
        next(row for row in profile['plugins'] if row['id'] == 'store')['config']['root'] = str(self.root / 'hosted')
        tools = () if effect is None else (ToolPlugin('effect', 'Synthetic local effect', {
            'type': 'object', 'properties': {}, 'additionalProperties': False}, effect),)
        app, host = create_agent_application(decide=decide, tools=tools, profile=profile, environment={})
        self.addCleanup(host.close)
        return app, host

    @staticmethod
    def turn(app, sid, request='turn', revision=0):
        return app.turn(OWNER, sid, message='synthetic question', request_id=request, expected_revision=revision)

    def test_direct_finish_ack_loss_leaves_durable_result_and_cleanup_cannot_rewrite_it(self):
        store, adapter, sid, context = self.direct()
        session = adapter.begin(context, 'question')
        session.append([{'role': 'assistant', 'content': {'answer': 'candidate'}}])
        self.assertFalse(any(event['kind'] == 'assistant' for event in store.events(OWNER, sid)))
        with lost_ack(store), self.assertRaises(SessionPersistenceError) as raised:
            session.finish({'answer': 'candidate'})
        self.assertIsInstance(raised.exception.__cause__, CommitUnknown)
        self.assertFalse(session._closed)
        durable = rows(store)
        self.assertEqual(store.load(OWNER, sid)['state']['turn_number'], 1)
        self.assertEqual([row['kind'] for row in store.events(OWNER, sid)], ['message', 'assistant', 'turn_completed'])
        with self.assertRaises(PlatformError) as cleanup:
            session.fail(raised.exception)
        self.assertEqual(cleanup.exception.code, 'TURN_LEASE_EXPIRED')
        self.assertEqual(rows(store), durable)
        replay = adapter.begin(context, 'question')
        self.assertIsNone(replay._token)
        saved = replay.finish('ignored replacement')
        self.assertEqual(saved['status'], 'complete')
        self.assertEqual(saved['result'], {'answer': 'candidate'})
        self.assertEqual(rows(store), durable)
        with self.assertRaisesRegex(RuntimeError, 'SESSION_CLOSED'):
            replay.append([{'role': 'user', 'content': 'forbidden'}])

    def test_public_finish_ack_loss_preserves_completed_turn_and_cached_replay_runs_no_tool(self):
        calls, effects = [], []

        def decide(messages, tools):
            calls.append(1)
            return ToolCall('effect', {}) if messages[-1]['role'] == 'user' else Answer('saved answer')

        app, host = self.application(decide, lambda context, arguments: effects.append(context.call_id) or ToolResult({}))
        sid = app.create(OWNER)['session_id']
        store = host.service('session.store')
        original = store.finish
        durable = []

        def finish(*args, **kwargs):
            try:
                with lost_ack(store):
                    return original(*args, **kwargs)
            except CommitUnknown:
                durable.append(rows(store))
                raise

        with patch.object(store, 'finish', side_effect=finish), self.assertRaises(SessionPersistenceError):
            self.turn(app, sid)
        self.assertEqual(rows(store), durable[0])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(effects), 1)
        saved = self.turn(app, sid)
        self.assertEqual(saved['status'], 'complete')
        self.assertEqual(saved['result'], 'saved answer')
        self.assertEqual(rows(store), durable[0])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(effects), 1)

    def test_public_tool_record_ack_loss_is_unknown_even_with_zero_actual_tool_effects(self):
        calls, effects = [], []
        app, host = self.application(lambda *args: calls.append(1) or ToolCall('effect', {}),
            lambda context, arguments: effects.append(context.call_id) or ToolResult({}))
        sid = app.create(OWNER)['session_id']
        store = host.service('session.store')
        original = store.append

        def append(actor, session_id, token, kind, payload, group_id, agent='orchestrator'):
            if 'tool_call' in payload:
                with lost_ack(store):
                    return original(actor, session_id, token, kind, payload, group_id, agent)
            return original(actor, session_id, token, kind, payload, group_id, agent)

        with patch.object(store, 'append', side_effect=append), self.assertRaises(SessionPersistenceError):
            self.turn(app, sid)
        self.assertEqual(calls, [1])
        self.assertEqual(effects, [])
        before = rows(store)
        cached = self.turn(app, sid)
        self.assertEqual(cached['status'], 'error')
        self.assertEqual(rows(store), before)
        with self.assertRaises(PlatformError) as raised:
            self.turn(app, sid, request='new', revision=cached['revision'])
        self.assertEqual(raised.exception.code, 'SESSION_OUTCOME_UNKNOWN')
        self.assertEqual(rows(store), before)
        self.assertEqual(calls, [1])
        self.assertEqual(effects, [])

    def test_public_unknown_tool_effect_is_neither_replayed_nor_reclassified_as_zero(self):
        calls, effects = [], []

        def effect(context, arguments):
            effects.append(context.call_id)
            raise RuntimeError('synthetic unknown tool result')

        app, host = self.application(lambda *args: calls.append(1) or ToolCall('effect', {}), effect)
        sid = app.create(OWNER)['session_id']
        failed = self.turn(app, sid)
        self.assertEqual(failed['status'], 'error')
        before = rows(host.service('session.store'))
        self.assertEqual(self.turn(app, sid), failed)
        with self.assertRaises(PlatformError) as raised:
            self.turn(app, sid, request='new', revision=failed['revision'])
        self.assertEqual(raised.exception.code, 'SESSION_OUTCOME_UNKNOWN')
        self.assertEqual(rows(host.service('session.store')), before)
        self.assertEqual(calls, [1])
        self.assertEqual(len(effects), 1)

    def test_public_user_record_ack_loss_settles_failure_without_any_model_call(self):
        calls = []
        app, host = self.application(lambda *args: calls.append(1) or Answer('unexpected'))
        sid = app.create(OWNER)['session_id']
        store = host.service('session.store')
        original = store.append

        def append(*args, **kwargs):
            with lost_ack(store):
                return original(*args, **kwargs)

        with patch.object(store, 'append', side_effect=append), self.assertRaises(SessionPersistenceError):
            self.turn(app, sid)
        self.assertEqual(calls, [])
        self.assertEqual([event['kind'] for event in store.events(OWNER, sid)], ['message', 'turn_failed', 'turn_completed'])
        before = rows(store)
        cached = self.turn(app, sid)
        self.assertEqual(cached['status'], 'error')
        self.assertEqual(rows(store), before)
        self.assertEqual(calls, [])

    def test_direct_final_buffer_and_messages_are_copied_and_close_only_after_commit(self):
        store, adapter, sid, context = self.direct()
        message = {'text': ['question']}
        session = adapter.begin(context, message)
        message['text'].append('caller mutation')
        record = {'role': 'assistant', 'content': {'answer': ['candidate']}}
        session.append([record])
        record['content']['answer'].append('caller mutation')
        returned = session.messages()
        returned[0]['content']['text'].append('observer mutation')
        self.assertEqual(session.messages()[0]['content'], {'text': ['question']})
        self.assertFalse(session._closed)
        response = session.finish({'answer': 'candidate'})
        self.assertTrue(session._closed)
        self.assertEqual(session.messages(), [])
        self.assertEqual(response['revision'], 1)
        assistant = next(event['payload'] for event in store.events(OWNER, sid) if event['kind'] == 'assistant')
        self.assertEqual(assistant, {'role': 'assistant', 'content': {'answer': ['candidate']}})

    def test_initialization_baseexception_is_preserved_after_real_failure_cleanup(self):
        class SyntheticAbort(BaseException):
            code = 'SYNTHETIC_ABORT'

        store, adapter, sid, context = self.direct()
        failure = SyntheticAbort('synthetic cancellation')
        original = store.events
        count = 0

        def history(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise failure
            return original(*args)

        with patch.object(store, 'events', side_effect=history), self.assertRaises(SyntheticAbort) as raised:
            adapter.begin(context, 'question')
        self.assertIs(raised.exception, failure)
        self.assertEqual(store.load(OWNER, sid)['revision'], 1)
        self.assertEqual([event['kind'] for event in store.events(OWNER, sid)], ['turn_failed', 'turn_completed'])
        cached = adapter.begin(context, 'question').finish('ignored')
        self.assertEqual(cached['stop_reason'], 'SYNTHETIC_ABORT')
        self.assertEqual(cached['status'], 'error')


if __name__ == '__main__':
    unittest.main()
