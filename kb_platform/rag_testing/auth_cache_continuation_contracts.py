from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.authentication import SessionAuthentication
from kb_platform.knowledge_contracts import EVIDENCE_METHODS, EvidenceEndpoint
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.session_identity import Actor

from .authentication_contracts import MemorySessions
from .cache import StageCache
from .volatile_stage_cache_contracts import stage_key


class AuthenticationReadBoundaryContracts(unittest.TestCase):
    def test_expiry_is_checked_after_store_read_but_revoke_after_read_does_not_rewrite_snapshot(self):
        for expires_during_read in (False, True):
            with self.subTest(expires_during_read=expires_during_read):
                store, now = MemorySessions(), [0]
                auth = SessionAuthentication(store, clock=lambda: now[0])
                actor = Actor('tenant', 'alice')
                session = auth.issue(actor, ttl_seconds=10)
                original = store.get

                def read_then_revoke(key):
                    value = original(key)
                    auth.revoke(session.token)
                    if expires_during_read:
                        now[0] = 10
                    return value

                store.get = read_then_revoke
                self.assertEqual(auth.authenticate(session.token), None if expires_during_read else actor)
                self.assertIsNone(auth.authenticate(session.token))

    def test_token_keys_isolate_tenant_and_principal_pairs_and_revocation(self):
        store = MemorySessions()
        auth = SessionAuthentication(store, clock=lambda: 0)
        actors = (Actor('first', 'alice'), Actor('second', 'alice'), Actor('first', 'bob'))
        sessions = [auth.issue(actor, ttl_seconds=10) for actor in actors]
        self.assertEqual(len(store.values), 3)
        self.assertEqual([auth.authenticate(session.token) for session in sessions], list(actors))
        self.assertTrue(auth.revoke(sessions[0].token))
        self.assertIsNone(auth.authenticate(sessions[0].token))
        self.assertEqual([auth.authenticate(session.token) for session in sessions[1:]], list(actors[1:]))


class CacheAuthorizationBoundaryContracts(unittest.TestCase):
    def test_authorization_linearizes_at_admission_not_at_payload_return(self):
        authorized, checks = [True], []

        class Objects:
            def put(self, name, value):
                self.raw = value
                return SimpleNamespace(locator='synthetic-object')

            def read(self, name, locator):
                authorized[0] = False
                return self.raw

        def authorize():
            checks.append(authorized[0])
            return authorized[0]

        with TemporaryDirectory() as directory:
            cache = StageCache(Path(directory), objects=Objects())
            try:
                cache.put(stage_key(), {'synthetic': 'immutable'})
                self.assertEqual(cache.get(stage_key(), authorize=authorize), {'synthetic': 'immutable'})
                with self.assertRaises(PermissionError):
                    cache.get(stage_key(), authorize=authorize)
                self.assertEqual(checks, [True, False])
            finally:
                cache.close()


class EvidenceContinuationContracts(unittest.TestCase):
    def setUp(self):
        self.now, self.disposed = 0, []
        self.actor = asdict(Actor('tenant', 'alice'))
        self.endpoint = EvidenceEndpoint(SimpleNamespace())
        self.addCleanup(self.endpoint.close)
        self.wire = ServiceEndpoint(self.endpoint, EVIDENCE_METHODS)
        clock = patch('kb_platform.knowledge_contracts.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def prepare(self, outcome='success'):
        def staged(*args, **kwargs):
            try:
                ranking = yield {'candidates': [{'synthetic': True}], 'assessment_input': 'synthetic'}
                if outcome == 'resume-error':
                    raise ValueError('SYNTHETIC_RESUME_FAILURE')
                if outcome == 'unexpected-yield':
                    yield []
                return {'ranking': ranking}
            finally:
                self.disposed.append(outcome)

        with patch('kb_platform.retrieval_pipeline.staged', side_effect=staged):
            return self.wire.prepare_pipeline({'actor': self.actor, 'scope_id': 'scope', 'query': 'synthetic',
                                               'policy': {}})

    def test_wrong_principal_and_tenant_cannot_consume_or_cancel_another_continuation(self):
        prepared = self.prepare()
        for actor in (Actor('other', 'alice'), Actor('tenant', 'bob')):
            for options in ({'ranking': []}, {'cancel': True}):
                with self.subTest(actor=actor, options=options), self.assertRaises(ContractError) as caught:
                    self.wire.finish_pipeline({'token': prepared['token'], 'actor': asdict(actor), **options})
                self.assertEqual(caught.exception.code, 'RETRIEVAL_STAGE_NOT_FOUND')
                self.assertEqual(self.disposed, [])
        self.assertEqual(self.wire.finish_pipeline({'token': prepared['token'], 'actor': self.actor,
                                                   'ranking': []}), {'ranking': []})
        self.assertEqual(self.disposed, ['success'])

    def test_each_owned_terminal_outcome_consumes_and_closes_exactly_once(self):
        for outcome in ('success', 'cancel', 'expired', 'resume-error', 'unexpected-yield', 'close'):
            with self.subTest(outcome=outcome):
                prepared = self.prepare(outcome)
                request = {'token': prepared['token'], 'actor': self.actor,
                           **({'cancel': True} if outcome == 'cancel' else {'ranking': []})}
                if outcome == 'expired':
                    self.now += 901
                if outcome == 'close':
                    self.endpoint.close()
                if outcome in ('success', 'cancel'):
                    self.assertEqual(self.wire.finish_pipeline(request), {} if outcome == 'cancel' else {'ranking': []})
                else:
                    with self.assertRaises(ContractError):
                        self.wire.finish_pipeline(request)
                self.assertEqual(self.disposed.count(outcome), 1)
                with self.assertRaises(ContractError) as caught:
                    self.wire.finish_pipeline(request)
                self.assertEqual(caught.exception.code, 'RETRIEVAL_STAGE_NOT_FOUND')
                self.assertEqual(self.disposed.count(outcome), 1)

    def test_exact_deadline_is_live_and_next_prepare_purges_expired_continuations(self):
        first = self.prepare('exact-deadline')
        self.now = 900
        self.assertEqual(self.wire.finish_pipeline({'actor': self.actor, 'token': first['token'],
                                                   'ranking': []}), {'ranking': []})
        expired = self.prepare('purged')
        self.now = 1801
        self.prepare('remaining')
        self.assertIn('purged', self.disposed)
        with self.assertRaises(ContractError) as caught:
            self.wire.finish_pipeline({'actor': self.actor, 'token': expired['token'], 'ranking': []})
        self.assertEqual(caught.exception.code, 'RETRIEVAL_STAGE_NOT_FOUND')
