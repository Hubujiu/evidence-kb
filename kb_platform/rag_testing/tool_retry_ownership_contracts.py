from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from tenacity import TryAgain

from kb_platform.knowledge_planning import KnowledgePlanning
from kb_platform.model_errors import RemoteModelError
from kb_platform.provider_runtime import RuntimePolicyError, ToolExecutor
from kb_platform.rag_testing import knowledge_platform_contracts


class ToolRetryOwnershipContracts(unittest.TestCase):
    def sqlite_case(self, *, read_only, unknown):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'effects.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE effects (value INTEGER)')
            sleeps = []
            executor = ToolExecutor(max_attempts=2, max_wait_seconds=2, sleep=sleeps.append)

            def operation():
                with sqlite3.connect(path) as connection:
                    connection.execute('INSERT INTO effects VALUES (1)')
                    count = connection.execute('SELECT COUNT(*) FROM effects').fetchone()[0]
                if count == 1:
                    if unknown:
                        raise TryAgain() from httpx.ReadTimeout('effect committed, result unknown')
                    raise TryAgain()
                return {'count': count}

            result = executor.execute('session:turn:identity', 'mutating-fixture', {}, operation, read_only=read_only)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM effects').fetchone()[0], 1)
            self.assertEqual((result.status, result.attempts), ('UNKNOWN' if unknown else 'ERROR', 1))
            self.assertEqual(sleeps, [])
            denied = Mock(side_effect=AssertionError('terminal receipt must not replay'))
            reused = executor.execute('session:turn:identity', 'mutating-fixture', {}, denied, read_only=True)
            self.assertTrue(reused.reused)
            self.assertEqual((reused.status, reused.attempts), (result.status, 1))
            denied.assert_not_called()

    def test_sqlite_mutation_cannot_use_explicit_retry_to_bypass_nonreadonly_policy(self):
        self.sqlite_case(read_only=False, unknown=False)

    def test_sqlite_unknown_effect_is_not_replayed_even_by_explicit_retry(self):
        self.sqlite_case(read_only=True, unknown=True)

    def test_explicit_retry_of_confirmed_not_sent_read_only_work_still_respects_attempt_cap(self):
        calls, sleeps = [], []

        def operation():
            calls.append('actual call')
            raise TryAgain() from httpx.ConnectError('confirmed not sent')

        result = ToolExecutor(max_attempts=3, max_wait_seconds=2, sleep=sleeps.append).execute(
            'session:turn:read', 'read', {}, operation, read_only=True)
        self.assertEqual((result.status, result.attempts, len(calls)), ('ERROR', 3, 3))
        self.assertEqual(sleeps, [.25, .5])

    def test_public_platform_error_retry_hint_blocks_retry_beyond_wait_budget(self):
        fixture = knowledge_platform_contracts.KnowledgePlatformContracts()
        self.addCleanup(fixture.doCleanups)
        case = fixture.fixture()
        case.platform.search.side_effect = RemoteModelError('PROVIDER_RATE_LIMITED', 429, retry_after_seconds=100)
        sleeps = []
        executor = ToolExecutor(max_attempts=2, max_wait_seconds=2, sleep=sleeps.append)
        result = executor.execute('session:turn:search', 'search', {'query': 'original'},
            lambda: case.client.search(case.actor, 'kb-a', 'original'), read_only=True)
        self.assertEqual((result.status, result.attempts, case.platform.search.call_count), ('ERROR', 1, 1))
        self.assertEqual(sleeps, [])
        self.assertEqual(result.category, 'rate_limit')

    def test_public_hint_cumulative_wait_and_deadline_are_checked_before_sleep(self):
        for max_wait, deadline, expected_calls, expected_sleeps in [(1, None, 2, [.6]), (2, .6, 1, [])]:
            with self.subTest(deadline=deadline), patch('kb_platform.provider_runtime.time.monotonic', return_value=10):
                operation = Mock(side_effect=RemoteModelError('THROTTLED', 429, retry_after_seconds=.6))
                sleeps = []
                result = ToolExecutor(max_attempts=3, max_wait_seconds=max_wait, sleep=sleeps.append).execute(
                    'session:turn:hint', 'read', {}, operation, read_only=True, deadline_seconds=deadline)
                self.assertEqual((result.attempts, operation.call_count), (expected_calls, expected_calls))
                self.assertEqual(sleeps, expected_sleeps)

    def test_invalid_public_hints_do_not_create_invalid_waits(self):
        for hint in (None, -1, float('nan'), float('inf'), float('-inf'), True, '100', 10 ** 1000):
            with self.subTest(hint=hint):
                operation = Mock(side_effect=[RemoteModelError('THROTTLED', 429, retry_after_seconds=hint), 'done'])
                sleeps = []
                result = ToolExecutor(max_attempts=2, sleep=sleeps.append).execute(
                    'session:turn:hint', 'read', {}, operation, read_only=True)
                self.assertEqual((result.status, result.attempts), ('COMPLETE', 2))
                self.assertEqual(sleeps, [.25])

    def test_claim_is_exclusive_and_terminal_reuse_returns_an_isolated_snapshot(self):
        entered, release = Event(), Event()
        executor, result = ToolExecutor(), Future()

        def operation():
            entered.set()
            if not release.wait(3):
                raise RuntimeError('TOOL_FIXTURE_BARRIER_EXPIRED')
            return {'items': ['original']}

        def call():
            try:
                result.set_result(executor.execute('s:t:id', 'read', {'q': 'original'}, operation, read_only=True))
            except BaseException as error:
                result.set_exception(error)

        thread = Thread(target=call)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            for args, code in [({'q': 'original'}, 'TOOL_REQUEST_IN_PROGRESS'), ({'q': 'changed'}, 'TOOL_REQUEST_ID_REUSED')]:
                with self.assertRaises(RuntimePolicyError) as caught:
                    executor.execute('s:t:id', 'read', args, Mock(), read_only=True)
                self.assertEqual(caught.exception.code, code)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        result.result().value['items'].append('caller mutation')
        reused = executor.execute('s:t:id', 'read', {'q': 'original'}, Mock())
        self.assertTrue(reused.reused)
        self.assertEqual(reused.value, {'items': ['original']})

    def test_real_planning_caller_namespaces_receipts_by_session_turn_and_identity(self):
        executor, operation = ToolExecutor(), Mock(return_value='read result')
        service = SimpleNamespace(gateway=SimpleNamespace(authorize=Mock(return_value={'revision': 1})),
            tool_context=lambda _: nullcontext(), plugins={}, tool_executor=executor)
        for session, turn in [('one', 'first'), ('one', 'second'), ('two', 'first'), ('one', 'first')]:
            planning = SimpleNamespace(config={'robust_tools': True}, service=service, actor=object(), scope='scope',
                authority={'revision': 1}, session_id=session, request_id=turn, trace={},
                execution_limits={'max_turn_seconds': 30}, started=0)
            with patch('kb_platform.knowledge_planning.time.perf_counter', return_value=1):
                self.assertEqual(KnowledgePlanning.invoke_tool(planning, 'read', {'citation_ids': ['known']}, operation),
                                 'read result')
        self.assertEqual(operation.call_count, 3)
