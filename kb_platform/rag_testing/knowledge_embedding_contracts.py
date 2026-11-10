from hashlib import sha256
import unittest
from types import SimpleNamespace

from kb_platform.agent_tools import EvidenceGateway
from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.documents import parse_native_document
from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.embedding_jobs import EmbeddingJobs
from kb_platform.knowledge_access import KnowledgeAccess
from kb_platform.knowledge_contracts import EVIDENCE_METHODS, EvidenceClient, EvidenceEndpoint
from kb_platform.knowledge_model_ports import AuxiliaryModelEndpoint, EmbeddingClient, METHODS
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.plugin_contracts import ServiceEndpoint
from kb_platform.storage import OriginalStore
from . import knowledge_persistence_contracts


class KnowledgeEmbeddingContracts(unittest.TestCase):
    def setUp(self):
        knowledge_persistence_contracts.KnowledgePersistenceContracts.setUp(self)

    def public_embedding(self, repository, name, *, resume=False):
        access = KnowledgeAccess(repository)
        calls = []

        def documents(texts, *, request_id, audit_context, authorize):
            self.assertTrue(authorize())
            calls.append(('document', list(texts), request_id, audit_context))
            return [[1., 0.] for _ in texts]

        def query(text, *, request_id, audit_context, authorize):
            self.assertTrue(authorize())
            calls.append(('query', [text], request_id, audit_context))
            return [1., 0.]

        provider = SimpleNamespace(provider_id='offline-model', model='fixed', dimension=2,
            max_batch_size=1, manifest={'provider': 'offline-model', 'model': 'fixed', 'dimension': 2},
            documents=documents, query=query)
        endpoint = AuxiliaryModelEndpoint(embedding=provider, authorization=access)
        self.addCleanup(endpoint.close)
        dependency = ServiceEndpoint(endpoint, METHODS)
        client = EmbeddingClient(dependency, dependency.metadata({})['embedding'])
        search = EmbeddedSearch(self.root / name)
        search.supports_resume_build = resume
        self.addCleanup(search.close)
        platform = Platform(repository=repository, search=search,
            store=OriginalStore(self.root / 'originals'), parser=parse_native_document,
            embedding_jobs=EmbeddingJobs(repository), embedding_provider=client)
        evidence = EvidenceEndpoint(EvidenceGateway(platform, provider_id='fixture-model', embedding_provider=client))
        self.addCleanup(evidence.close)
        return platform, EvidenceClient(ServiceEndpoint(evidence, EVIDENCE_METHODS)), client, access, calls

    def test_public_dense_queries_keep_published_version_through_candidate_update_and_publish(self):
        actor = Actor('published-embedding', 'owner')
        repository = KnowledgeRepository(database=self.database)
        for resume in (False, True):
            with self.subTest(resume=resume):
                platform, evidence, _, _, calls = self.public_embedding(repository,
                    'published-' + str(resume), resume=resume)
                scope = platform.create_kb(actor, 'Synthetic published embedding lifecycle')['kb_id']
                original_text, candidate_text = 'QUARTZMARKER value 42.', 'QUARTZMARKER value 99.'
                original = platform.ingest(actor, scope, 'source.txt', original_text.encode(), policy={
                    'embedding': True, 'llm_context': True, 'allowed_providers': ['offline-model', 'fixture-model']})
                original_build = platform.stage(actor, scope, ['dense', 'hybrid'])
                platform.publish(actor, scope, original_build['build_id'])

                def check_published(source, build, text):
                    before = len(calls)
                    for channel in ('dense', 'hybrid'):
                        result = platform.search(actor, scope, 'QUARTZMARKER', channel=channel)
                        self.assertEqual(result['context'], text)
                        self.assertEqual(result['manifest_id'], build['build_id'])
                        self.assertTrue(result['trace']['model_query_executed'])
                        self.assertTrue(all(hit['source_version_id'] == source['source_version_id']
                                            for hit in result['hits']))
                    result = evidence.search_pipeline(actor, scope, 'QUARTZMARKER', {},
                        {'channels': ['dense'], 'parent': 'none', 'rerank': False, 'title_navigation': False})
                    self.assertTrue(result['hits'])
                    self.assertTrue(all(hit['text'] == text and
                        hit['source_version_id'] == source['source_version_id'] for hit in result['hits']))
                    self.assertEqual([call[0] for call in calls[before:]], ['query'] * 3)
                    self.assertEqual([call[3]['purpose'] for call in calls[before:]],
                                     ['human_search', 'human_search', 'llm_context'])
                    for _, _, _, audit in calls[before:]:
                        self.assertEqual(audit['build_id'], build['build_id'])
                        self.assertEqual(audit['sources'], [{'source_id': source['source_id'],
                            'source_version_id': source['source_version_id'],
                            'parse_snapshot_id': source['document']['parse_snapshot_id'],
                            'source_text_sha256': sha256(text.encode()).hexdigest()}])

                check_published(original, original_build, original_text)
                candidate = platform.ingest(actor, scope, 'source.txt', candidate_text.encode(),
                    source_id=original['source_id'])
                self.assertNotEqual(candidate['source_version_id'], original['source_version_id'])
                check_published(original, original_build, original_text)
                candidate_build = platform.stage(actor, scope, ['dense', 'hybrid'])
                document_calls = [call for call in calls if call[0] == 'document']
                self.assertEqual([call[1] for call in document_calls], [[original_text], [candidate_text]])
                self.assertEqual(document_calls[-1][3]['build_id'], candidate_build['build_id'])
                self.assertEqual(document_calls[-1][3]['sources'][0]['source_version_id'],
                                 candidate['source_version_id'])
                check_published(original, original_build, original_text)
                platform.publish(actor, scope, candidate_build['build_id'])
                check_published(candidate, candidate_build, candidate_text)
                self.assertEqual(len({call[2] for call in calls}), len(calls))

    def test_public_query_rechecks_live_revocation_of_published_sources(self):
        actor, reader = Actor('published-revocation', 'owner'), Actor('published-revocation', 'reader')
        repository = KnowledgeRepository(database=self.database)
        for revocation in ('readers', 'binding', 'embedding', 'llm_context', 'provider'):
            with self.subTest(revocation=revocation):
                platform, evidence, client, access, calls = self.public_embedding(repository, 'revocation-' + revocation)
                scope = platform.create_kb(actor, 'Synthetic live revocation')['kb_id']
                platform.set_member(actor, scope, reader.principal_id, ['kb.read'])
                policy = {'embedding': True, 'llm_context': True,
                          'allowed_providers': ['offline-model', 'fixture-model']}
                source = platform.ingest(actor, scope, 'source.txt', b'QUARTZMARKER value 42.',
                    readers=[reader.principal_id], policy=policy)
                build = platform.stage(actor, scope, ['dense'])
                platform.publish(actor, scope, build['build_id'])
                config = {'channels': ['dense'], 'parent': 'none', 'rerank': False, 'title_navigation': False}
                self.assertTrue(evidence.search_pipeline(reader, scope, 'QUARTZMARKER', {}, config)['hits'])
                original_audit = calls[-1][3]
                platform.ingest(actor, scope, 'source.txt', b'QUARTZMARKER value 99.', source_id=source['source_id'])
                self.assertTrue(evidence.search_pipeline(reader, scope, 'QUARTZMARKER', {}, config)['hits'])
                if revocation == 'readers':
                    platform.set_source_readers(actor, source['source_id'], [])
                elif revocation == 'binding':
                    platform.remove_binding(actor, scope, source['binding_id'])
                else:
                    revoked = {**policy, revocation: False} if revocation != 'provider' else {
                        **policy, 'allowed_providers': ['fixture-model']}
                    platform.set_source_policy(actor, source['source_id'], revoked)
                before = len(calls)
                with self.assertRaises(PlatformError) as caught:
                    client.query('QUARTZMARKER', request_id='revoked-old-' + revocation,
                                 audit_context=original_audit)
                self.assertEqual(caught.exception.code, 'STALE_BUILD')
                authority = access.authorize({'actor': {'tenant_id': reader.tenant_id,
                    'principal_id': reader.principal_id}, 'scope_id': scope})
                fresh_audit = {**original_audit, 'acl_revision': str(authority['authority_revision'])}
                with self.assertRaises(PlatformError) as caught:
                    client.query('QUARTZMARKER', request_id='revoked-fresh-' + revocation,
                                 audit_context=fresh_audit)
                self.assertEqual(caught.exception.code,
                    'PROCESSING_NOT_ALLOWED' if revocation in {'embedding', 'provider'} else 'ACCESS_DENIED')
                if revocation in {'embedding', 'provider'}:
                    with self.assertRaises(PlatformError) as caught:
                        evidence.search_pipeline(reader, scope, 'QUARTZMARKER', {}, config)
                    self.assertEqual(caught.exception.code, 'PROCESSING_NOT_ALLOWED')
                else:
                    self.assertEqual(evidence.search_pipeline(reader, scope, 'QUARTZMARKER', {}, config)['hits'], [])
                self.assertEqual(len(calls), before)

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
