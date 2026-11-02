from hashlib import sha256
import unittest
from types import SimpleNamespace

from kb_platform.agent_tools import EvidenceGateway
from kb_platform.core import Actor, Platform
from kb_platform.documents import parse_native_document
from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.embedding_jobs import EmbeddingJobs
from kb_platform.knowledge_access import KnowledgeAccess
from kb_platform.knowledge_model_ports import AuxiliaryModelEndpoint, EmbeddingClient, METHODS
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.plugin_contracts import ServiceEndpoint
from kb_platform.storage import OriginalStore
from . import knowledge_persistence_contracts


class KnowledgeEmbeddingContracts(unittest.TestCase):
    def setUp(self):
        knowledge_persistence_contracts.KnowledgePersistenceContracts.setUp(self)

    def test_build_http_dense_and_agent_dense_use_authorized_public_model_calls(self):
        repository = KnowledgeRepository(database=self.database)
        access = KnowledgeAccess(repository)
        actor = Actor('wire-embedding', 'owner')
        calls = []

        def documents(texts, *, request_id, audit_context, authorize):
            self.assertTrue(authorize())
            calls.append(('document', request_id, audit_context))
            return [[1., 0.] for _ in texts]

        def query(text, *, request_id, audit_context, authorize):
            self.assertTrue(authorize())
            calls.append(('query', request_id, audit_context))
            return [1., 0.]

        provider = SimpleNamespace(provider_id='offline-model', model='fixed', dimension=2,
            max_batch_size=1, manifest={'provider': 'offline-model', 'model': 'fixed', 'dimension': 2},
            documents=documents, query=query)
        endpoint = AuxiliaryModelEndpoint(embedding=provider, authorization=access)
        self.addCleanup(endpoint.close)
        dependency = ServiceEndpoint(endpoint, METHODS)
        client = EmbeddingClient(dependency, dependency.metadata({})['embedding'])
        for resume in (False, True):
            with self.subTest(resume=resume):
                search = EmbeddedSearch(self.root / ('search-' + str(resume)))
                search.supports_resume_build = resume
                self.addCleanup(search.close)
                platform = Platform(repository=repository, search=search,
                    store=OriginalStore(self.root / 'originals'), parser=parse_native_document,
                    embedding_jobs=EmbeddingJobs(repository), embedding_provider=client)
                scope = platform.create_kb(actor, 'Audited public embedding')['kb_id']
                text = b'Explicit synthetic policy for public plugin contracts.'
                source = platform.ingest(actor, scope, 'source.txt', text, policy={
                    'embedding': True, 'llm_context': True, 'allowed_providers': ['offline-model', 'fixture-model']})
                build = platform.stage(actor, scope, ['dense'])
                platform.publish(actor, scope, build['build_id'])
                result = platform.search(actor, scope, 'policy', channel='dense')
                self.assertEqual(result['context'], text.decode())
                gateway = EvidenceGateway(platform, provider_id='fixture-model', embedding_provider=client)
                result = gateway.search_pipeline(actor, scope, 'policy', {}, {'channels': ['dense'], 'parent': 'none'})
                self.assertTrue(result['hits'])
                matching = [row for row in calls if row[2]['kb_id'] == scope]
                self.assertEqual([row[0] for row in matching], ['document', 'query', 'query'])
                self.assertEqual(len({row[1] for row in matching}), 3)
                self.assertEqual([row[2]['purpose'] for row in matching[1:]], ['human_search', 'llm_context'])
                for _, request_id, audit in matching:
                    self.assertTrue(request_id)
                    self.assertEqual(audit['principal'], actor.principal_id)
                    self.assertEqual(audit['build_id'], build['build_id'])
                    self.assertEqual(audit['sources'][0]['source_id'], source['source_id'])
                    self.assertEqual(audit['sources'][0]['source_text_sha256'], sha256(text).hexdigest())
                platform.set_source_policy(actor, source['source_id'], {
                    'embedding': True, 'llm_context': False, 'allowed_providers': ['offline-model']})
                self.assertEqual(platform.search(actor, scope, 'policy', channel='dense')['context'], text.decode())
