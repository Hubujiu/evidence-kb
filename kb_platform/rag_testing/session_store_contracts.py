from contextlib import closing
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kb_platform.knowledge_sessions import KnowledgeSessions
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_store import SessionStore


class SessionStoreContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.actor = Actor('tenant', 'owner')
        self.store = SessionStore(self.root)

    def create(self, state=None):
        return self.store.create(self.actor, 'general', state or {'namespace': 'generic/1'})

    def test_generic_state_round_trips_without_knowledge_fields_or_aliasing(self):
        initial = {'namespace': 'generic/1', 'todo': [{'text': 'calculate', 'complete': False}]}
        created = self.create(initial)
        initial['todo'][0]['text'] = 'caller mutation'
        reopened = SessionStore(self.root).load(self.actor, created['session_id'])
        self.assertEqual(reopened['state'], created['state'])
        self.assertEqual(set(reopened['state']), {'namespace', 'todo'})
        self.assertEqual(reopened['state']['todo'][0]['text'], 'calculate')
        with self.assertRaisesRegex(ValueError, 'INVALID_SESSION_STATE'):
            self.store.create(self.actor, 'general', [])

    def test_failed_commit_rolls_back_assistant_history_state_and_request_result(self):
        session = self.create()
        sid = session['session_id']
        lease = self.store.begin(self.actor, sid, 'request', 0, 'fingerprint')
        self.store.append(self.actor, sid, lease['token'], 'message',
                          {'role': 'user', 'content': '2 + 2'}, 'request')
        before = self.store.events(self.actor, sid)


        with self.assertRaises(TypeError):
            self.store.finish(self.actor, sid, lease['token'], {'invalid': object()},
                              {'status': 'complete', 'value': 4},
                              assistant_event={'role': 'assistant', 'content': '4'})
        self.assertEqual(self.store.load(self.actor, sid), session)
        self.assertEqual(self.store.events(self.actor, sid), before)
        with self.assertRaises(PlatformError) as caught:
            self.store.begin(self.actor, sid, 'request', 0, 'fingerprint')
        self.assertEqual(caught.exception.code, 'TURN_IN_PROGRESS')
        committed = self.store.finish(self.actor, sid, lease['token'], {'namespace': 'generic/1'},
                                      {'status': 'complete', 'value': 4},
                                      assistant_event={'role': 'assistant', 'content': '4'})
        reopened = SessionStore(self.root)
        self.assertEqual(reopened.begin(self.actor, sid, 'request', 0, 'fingerprint'), {'cached': committed})
        self.assertEqual([event['kind'] for event in reopened.events(self.actor, sid)],
                         ['message', 'assistant', 'turn_completed'])

    def test_generic_backend_reopens_knowledge_sessions_with_artifacts_and_replay(self):
        knowledge = KnowledgeSessions(SessionStore(self.root))
        session = knowledge.create(self.actor, 'knowledge-scope', {'mode': 'LOCKED'}, 'fixture')
        sid = session['session_id']
        lease = knowledge.begin(self.actor, sid, 'request', 0, 'fingerprint')
        artifact = knowledge.artifact(self.actor, sid, lease['token'], {'original': 'unchanged'})
        committed = knowledge.finish(self.actor, sid, lease['token'], session['state'],
                                  {'status': 'answered', 'answer': 'Existing answer'})
        with closing(sqlite3.connect(knowledge.path)) as db:
            schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        generic = SessionStore(self.root)
        self.assertEqual(generic.load(self.actor, sid)['state'], session['state'])
        self.assertEqual(generic.begin(self.actor, sid, 'request', 0, 'fingerprint'), {'cached': committed})
        self.assertEqual(generic.read_artifact(self.actor, sid, artifact['artifact_id']), {'original': 'unchanged'})
        with closing(sqlite3.connect(generic.path)) as db:
            self.assertEqual(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
                             schema)

    def test_principal_and_tenant_isolation_cover_history_artifacts_and_replay(self):
        sid = self.create()['session_id']
        lease = self.store.begin(self.actor, sid, 'request', 0, 'fingerprint')
        artifact = self.store.artifact(self.actor, sid, lease['token'], {'private': True})
        self.store.finish(self.actor, sid, lease['token'], {}, {'status': 'complete'})
        for actor in (Actor('tenant', 'other'), Actor('other', 'owner')):
            self.assertEqual(self.store.list_sessions(actor), [])
            for operation in (
                lambda: self.store.load(actor, sid),
                lambda: self.store.events(actor, sid),
                lambda: self.store.read_artifact(actor, sid, artifact['artifact_id']),
                lambda: self.store.begin(actor, sid, 'request', 0, 'fingerprint'),
            ):
                with self.subTest(actor=actor), self.assertRaises(PlatformError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, 'SESSION_NOT_FOUND')

    def test_failure_shape_is_owned_by_domain_adapter(self):
        session = self.create()
        sid = session['session_id']
        lease = self.store.begin(self.actor, sid, 'request', 0, 'fingerprint')
        response = {'status': 'error', 'stop_reason': 'GENERIC_FAILURE',
                    'evidence': {'meaning': 'arbitrary diagnostic field'}}
        result = self.store.fail(self.actor, sid, lease['token'], session['state'], response)
        self.assertEqual(result['evidence'], response['evidence'])
        self.assertEqual(self.store.events(self.actor, sid)[0]['payload'], {'code': 'GENERIC_FAILURE'})
        knowledge = KnowledgeSessions(SessionStore(self.root))
        with self.assertRaisesRegex(ValueError, 'INVALID_FAILURE_RESPONSE'):
            knowledge.fail(self.actor, sid, lease['token'], session['state'], response)

    def test_replaced_writer_cannot_commit_failure_into_new_owner_state(self):
        sid = self.create()['session_id']
        with patch('kb_platform.session_store.time.time', return_value=1000):
            old = self.store.begin(self.actor, sid, 'old', 0, 'old-fingerprint', lease_seconds=5)
        with patch('kb_platform.session_store.time.time', return_value=1006):
            replacement = SessionStore(self.root)
            new = replacement.begin(self.actor, sid, 'new', 0, 'new-fingerprint')
            with self.assertRaises(PlatformError) as caught:
                self.store.fail(self.actor, sid, old['token'], {'wrong': True},
                                {'status': 'error', 'stop_reason': 'LATE'})
            self.assertEqual(caught.exception.code, 'TURN_LEASE_EXPIRED')
            replacement.finish(self.actor, sid, new['token'], {'namespace': 'generic/1', 'new': True},
                               {'status': 'complete'})
        self.assertEqual(self.store.load(self.actor, sid)['state'], {'namespace': 'generic/1', 'new': True})

    def test_knowledge_facade_uses_injected_backend_and_persists_atomic_turn(self):
        class RecordingBackend:
            def __init__(self, backend):
                self.backend, self.calls = backend, []

            def __getattr__(self, name):
                value = getattr(self.backend, name)
                if not callable(value):
                    return value

                def invoke(*args, **kwargs):
                    self.calls.append(name)
                    return value(*args, **kwargs)
                return invoke

        backend = RecordingBackend(self.store)


        with patch.object(SessionStore, '__init__', side_effect=AssertionError('unexpected backend')):
            facade = KnowledgeSessions(backend)
            self.assertIs(facade.store, backend)
            session = facade.create(self.actor, 'scope', {'mode': 'AUTO'}, 'fixture')
            sid = session['session_id']
            admitted = facade.begin(self.actor, sid, 'turn', 0, 'fingerprint')
            state = {**admitted['session']['state'], 'turn_number': 1}
            result = facade.finish(self.actor, sid, admitted['token'], state,
                                   {'status': 'answered', 'answer': 'Preserved answer'},
                                   assistant_event={'text': 'Preserved answer'})
        knowledge = KnowledgeSessions(SessionStore(self.root))
        old = knowledge.create(self.actor, 'scope', {'mode': 'AUTO'}, 'fixture')
        self.assertEqual(session['state'], old['state'])
        self.assertEqual(knowledge.load(self.actor, sid)['state'], state)
        self.assertEqual(knowledge.begin(self.actor, sid, 'turn', 0, 'fingerprint'), {'cached': result})
        self.assertEqual([event['kind'] for event in knowledge.events(self.actor, sid)],
                         ['assistant', 'turn_completed'])
        self.assertEqual(backend.calls, ['create', 'begin', 'finish'])
        self.assertEqual(facade.root, self.store.root)

    def test_knowledge_facade_rejects_candidate_evidence_before_failure_commit(self):
        facade = KnowledgeSessions(self.store)
        session = facade.create(self.actor, 'scope', {})
        sid = session['session_id']
        admitted = facade.begin(self.actor, sid, 'turn', 0, 'fingerprint')
        invalid = {'status': 'error', 'stop_reason': 'FAILED', 'evidence': [{'unverified': True}]}
        with self.assertRaisesRegex(ValueError, 'INVALID_FAILURE_RESPONSE'):
            facade.fail(self.actor, sid, admitted['token'], session['state'], invalid)
        self.assertEqual(self.store.load(self.actor, sid)['revision'], 0)
        self.assertEqual(self.store.events(self.actor, sid), [])
        valid = {'status': 'error', 'stop_reason': 'FAILED', 'answer': '', 'evidence': [], 'claims': []}
        failed = facade.fail(self.actor, sid, admitted['token'], session['state'], valid)
        self.assertEqual(failed['revision'], 1)
        self.assertEqual(self.store.events(self.actor, sid)[0]['payload'], {'code': 'FAILED'})


if __name__ == '__main__':
    unittest.main()
