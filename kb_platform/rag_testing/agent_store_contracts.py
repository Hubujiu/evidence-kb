from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from kb_platform.knowledge_sessions import KnowledgeSessions
from kb_platform.session_store import SessionStore
from unittest.mock import patch

from kb_platform.core import Actor, PlatformError


class AgentStoreContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.actor = Actor('tenant', 'owner')
        self.first = KnowledgeSessions(SessionStore(self.root))
        self.second = KnowledgeSessions(SessionStore(self.root))
        self.session = self.first.create(self.actor, 'scope', {})['session_id']

    def concurrent_begins(self, sessions):
        barrier = Barrier(2, timeout=10)

        def begin(store, session, request):
            barrier.wait()
            try:
                return {'request': request, 'result': store.begin(
                    self.actor, session, request, 0, request + '-fingerprint')}
            except PlatformError as exc:
                return {'request': request, 'error': exc.code}

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(begin, store, session, request)
                       for store, session, request in zip(
                           (self.first, self.second), sessions, ('first', 'second'), strict=True)]
            return [future.result(timeout=15) for future in futures]

    def test_two_independent_stores_admit_only_one_concurrent_session_writer(self):
        outcomes = self.concurrent_begins((self.session, self.session))
        winners = [item for item in outcomes if 'result' in item]
        losers = [item for item in outcomes if 'error' in item]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0]['error'], 'TURN_IN_PROGRESS')
        self.assertTrue(winners[0]['result']['token'])
        self.assertEqual(self.first.load(self.actor, self.session)['revision'], 0)

    def test_concurrent_different_sessions_each_receive_a_lease(self):
        other = self.second.create(self.actor, 'scope', {})['session_id']
        outcomes = self.concurrent_begins((self.session, other))
        self.assertTrue(all('result' in item for item in outcomes), outcomes)
        tokens = [item['result']['token'] for item in outcomes]
        self.assertEqual(len(set(tokens)), 2)
        for store, session, outcome in zip(
                (self.first, self.second), (self.session, other), outcomes, strict=True):
            store.append(self.actor, session, outcome['result']['token'], 'user',
                         {'text': outcome['request']}, outcome['request'])
            self.assertEqual([event['payload']['text'] for event in store.events(self.actor, session)],
                             [outcome['request']])

    def test_completed_response_and_fingerprint_survive_store_restart(self):
        admitted = self.first.begin(self.actor, self.session, 'turn', 0, 'original-fingerprint')
        state = {**admitted['session']['state'], 'turn_number': 1}
        expected = self.first.finish(self.actor, self.session, admitted['token'], state,
                                     {'answer': 'synthetic answer', 'status': 'answered'})
        restarted = KnowledgeSessions(SessionStore(self.root))
        self.assertEqual(restarted.load(self.actor, self.session)['state'], state)
        self.assertEqual(restarted.begin(self.actor, self.session, 'turn', 0, 'original-fingerprint'),
                         {'cached': expected})
        with self.assertRaises(PlatformError) as caught:
            restarted.begin(self.actor, self.session, 'turn', 1, 'changed-fingerprint')
        self.assertEqual(caught.exception.code, 'REQUEST_ID_REUSED')
        events = restarted.events(self.actor, self.session)
        self.assertEqual([event['kind'] for event in events], ['turn_completed'])
        self.assertEqual(events[0]['payload'], expected)
        self.assertEqual(restarted.load(self.actor, self.session)['revision'], 1)

    def test_expired_writer_is_fenced_after_replacement_and_cannot_replay(self):
        with patch('kb_platform.session_store.time.time', return_value=1000):
            old = self.first.begin(self.actor, self.session, 'old', 0, 'old-fingerprint', lease_seconds=10)
            self.first.append(self.actor, self.session, old['token'], 'tool_call',
                              {'call_id': 'old-call', 'name': 'read'}, 'old')
        with patch('kb_platform.session_store.time.time', return_value=1011):
            new = self.second.begin(self.actor, self.session, 'new', 0, 'new-fingerprint')
            with self.assertRaises(PlatformError) as caught:
                self.first.append(self.actor, self.session, old['token'], 'tool_result',
                                  {'call_id': 'old-call'}, 'old')
            self.assertEqual(caught.exception.code, 'TURN_LEASE_EXPIRED')
            with self.assertRaises(PlatformError) as caught:
                self.first.finish(self.actor, self.session, old['token'], old['session']['state'],
                                  {'answer': 'late answer'})
            self.assertEqual(caught.exception.code, 'TURN_LEASE_EXPIRED')
            completed = self.second.finish(self.actor, self.session, new['token'], new['session']['state'],
                                           {'answer': 'replacement answer'})
            restarted = KnowledgeSessions(SessionStore(self.root))
            with self.assertRaises(PlatformError) as caught:
                restarted.begin(self.actor, self.session, 'old', 0, 'old-fingerprint')
            self.assertEqual(caught.exception.code, 'TURN_INTERRUPTED')
            self.assertEqual(restarted.begin(self.actor, self.session, 'new', 0, 'new-fingerprint'),
                             {'cached': completed})
            self.assertEqual(restarted.load(self.actor, self.session)['revision'], 1)
            events = restarted.events(self.actor, self.session)
            self.assertEqual([event['kind'] for event in events],
                             ['tool_call', 'interrupted', 'turn_completed'])
            self.assertEqual(events[1]['payload'], {'request_id': 'old'})
            self.assertEqual(events[2]['payload']['answer'], 'replacement answer')
