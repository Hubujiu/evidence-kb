from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from kb_platform.core import Platform
from kb_platform.database_contracts import CommitUnknown
from kb_platform.knowledge_access import KnowledgeAccess
from kb_platform.knowledge_contracts import SessionClient, SessionEndpoint, SESSION_OPERATIONS, methods
from kb_platform.knowledge_schema import KNOWLEDGE_BASES, SOURCES, knowledge_schema
from kb_platform.knowledge_sessions import KnowledgeSessions
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.plugin_contracts import ServiceEndpoint
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_store import SessionStore
from kb_platform.persistence_local import local_space


class KnowledgeCommitContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.backend = SessionStore(self.root, schema=knowledge_schema)
        self.addCleanup(self.backend.close)
        self.store = KnowledgeSessions(self.backend)
        database, service = local_space(self.root, self.backend.identity.space)
        self.addCleanup(service.close)
        self.repository = KnowledgeRepository(database=database)
        self.platform = Platform(repository=self.repository, search=None, store=None, parser=None, embedding_jobs=None)
        self.owner, self.actor = Actor('tenant', 'owner'), Actor('tenant', 'reader')
        self.scope = self.platform.create_kb(self.owner, 'Synthetic commit race')['kb_id']
        self.platform.set_member(self.owner, self.scope, self.actor.principal_id, ['kb.read'])
        self.authority = KnowledgeAccess(self.repository).authorize({'actor': asdict(self.actor), 'scope_id': self.scope})
        self.session = self.store.create(self.actor, self.scope, {})
        self.sid = self.session['session_id']
        self.token = self.store.begin(self.actor, self.sid, 'turn', 0, 'fingerprint')['token']

    def finish(self, store=None, *, authority=None, state=None):
        return (self.store if store is None else store).finish(self.actor, self.sid, self.token,
            self.session['state'] if state is None else state, {'status': 'answered', 'answer': 'Synthetic answer'},
            authority=self.authority if authority is None else authority,
            assistant_event={'text': 'Synthetic answer'})

    def test_revocation_before_commit_prevents_every_success_effect(self):
        self.platform.set_member(self.owner, self.scope, self.actor.principal_id, [])
        with self.assertRaisesRegex(PlatformError, 'Scope is not available'):
            self.finish()
        self.assertEqual(self.store.load(self.actor, self.sid), self.session)
        self.assertEqual(self.store.events(self.actor, self.sid), [])
        with self.assertRaises(PlatformError) as caught:
            self.store.begin(self.actor, self.sid, 'turn', 0, 'fingerprint')
        self.assertEqual(caught.exception.code, 'TURN_IN_PROGRESS')

    def test_manifest_only_change_invalidates_authorization_at_commit(self):
        with self.backend.database.transaction(scope={'tenant_id': self.actor.tenant_id, 'kb_id': self.scope}) as tx:
            tx.execute(KNOWLEDGE_BASES.update().where(KNOWLEDGE_BASES.c.tenant_id == self.actor.tenant_id,
                KNOWLEDGE_BASES.c.kb_id == self.scope).values(manifest_id='new-published-manifest'))
        with self.assertRaises(PlatformError) as caught:
            self.finish()
        self.assertEqual(caught.exception.code, 'SCOPE_CHANGED')
        self.assertEqual(self.store.events(self.actor, self.sid), [])

    def test_source_permission_revocation_invalidates_success_even_with_kb_membership(self):
        with self.backend.database.transaction(scope={'tenant_id': self.actor.tenant_id}) as tx:
            tx.insert(SOURCES, {'tenant_id': self.actor.tenant_id, 'source_id': 'synthetic-source',
                'owner_id': self.owner.principal_id, 'readers': [self.actor.principal_id], 'policy': {}, 'revision': 0})
        self.platform.set_source_readers(self.owner, 'synthetic-source', [])
        self.assertTrue(KnowledgeAccess(self.repository).authorize({'actor': asdict(self.actor), 'scope_id': self.scope}))
        with self.assertRaises(PlatformError) as caught:
            self.finish()
        self.assertEqual(caught.exception.code, 'SCOPE_CHANGED')
        self.assertEqual(self.store.load(self.actor, self.sid)['revision'], 0)
        self.assertEqual(self.store.events(self.actor, self.sid), [])

    def test_session_scope_cannot_be_replaced_by_an_authorized_other_scope(self):
        other = self.platform.create_kb(self.owner, 'Other scope')['kb_id']
        self.platform.set_member(self.owner, other, self.actor.principal_id, ['kb.read'])
        authority = KnowledgeAccess(self.repository).authorize({'actor': asdict(self.actor), 'scope_id': other})
        with self.assertRaises(PlatformError) as caught:
            self.finish(authority=authority)
        self.assertEqual(caught.exception.code, 'SCOPE_CHANGED')
        self.assertEqual(self.store.events(self.actor, self.sid), [])

    def test_commit_holds_authority_until_physical_commit_against_an_independent_client(self):
        second = SessionStore(self.root, schema=knowledge_schema)
        self.addCleanup(second.close)
        database, service = local_space(self.root, self.backend.identity.space)
        self.addCleanup(service.close)
        other = Platform(repository=KnowledgeRepository(database=database), search=None,
                         store=None, parser=None, embedding_jobs=None)
        ready, release, revoke_started, revoked = Event(), Event(), Event(), Event()
        original = self.backend.finish_in_transaction

        def staged(*args, **kwargs):
            result = original(*args, **kwargs)
            ready.set()
            if not release.wait(10):
                raise RuntimeError('COMMIT_BARRIER_TIMEOUT')
            return result

        def revoke():
            revoke_started.set()
            other.set_member(self.owner, self.scope, self.actor.principal_id, [])
            revoked.set()

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.backend, 'finish_in_transaction', staged):
            completion = pool.submit(self.finish)
            self.assertTrue(ready.wait(5))
            revocation = pool.submit(revoke)
            try:
                self.assertTrue(revoke_started.wait(5))
                self.assertFalse(revoked.wait(.1))
                self.assertEqual(second.load(self.actor, self.sid)['revision'], 0)
            finally:
                release.set()
            response = completion.result(timeout=10)
            revocation.result(timeout=10)
        self.assertTrue(revoked.is_set())
        self.assertEqual(response['revision'], 1)
        self.assertEqual([row['kind'] for row in self.store.events(self.actor, self.sid)],
                         ['assistant', 'turn_completed'])
        with self.assertRaises(PlatformError):
            KnowledgeAccess(self.repository).authorize({'actor': asdict(self.actor), 'scope_id': self.scope})

    def test_rollback_removes_all_staged_completion_effects_and_releases_authority(self):
        with self.assertRaises(TypeError):
            self.finish(state={'invalid': object()})
        self.assertEqual(self.store.events(self.actor, self.sid), [])
        self.assertEqual(self.store.load(self.actor, self.sid), self.session)
        self.platform.set_member(self.owner, self.scope, self.actor.principal_id, [])
        with self.assertRaises(PlatformError):
            self.finish()

    def test_lost_commit_ack_keeps_durable_result_and_operation_identity_across_public_boundary(self):
        client = SessionClient(ServiceEndpoint(SessionEndpoint(self.store), methods(SESSION_OPERATIONS)))
        original = Connection.commit
        commits = []

        def lose_ack(connection):
            original(connection)
            commits.append(True)
            raise DBAPIError('COMMIT', None, OSError('synthetic lost commit acknowledgement'), connection_invalidated=True)

        with patch.object(Connection, 'commit', lose_ack), self.assertRaises(PlatformError) as caught:
            self.finish(client)
        self.assertEqual(caught.exception.code, 'COMMIT_UNKNOWN')
        self.assertTrue(caught.exception.details['operation_id'])
        self.assertEqual(commits, [True])
        result = self.store.begin(self.actor, self.sid, 'turn', 0, 'fingerprint')['cached']
        self.assertEqual(result['revision'], 1)
        self.assertEqual([row['kind'] for row in self.store.events(self.actor, self.sid)],
                         ['assistant', 'turn_completed'])

    def test_direct_unknown_commit_preserves_the_unknown_exception(self):
        with patch.object(Connection, 'commit', side_effect=DBAPIError('COMMIT', None,
                OSError('synthetic unknown'), connection_invalidated=True)), self.assertRaises(CommitUnknown):
            self.finish()

