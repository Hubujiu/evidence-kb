from copy import deepcopy
from dataclasses import fields
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.session_identity import Actor, PlatformError
from kb_platform.documents import parse_document

from . import contract_application_contracts as applications


class DocumentManagementContracts(unittest.TestCase):
    compose = applications.ContractApplicationContracts.compose

    def setUp(self):
        applications.ContractApplicationContracts.setUp(self)
        parser = patch('kb_platform.document_parser.parse', parse_document)
        parser.start()
        self.addCleanup(parser.stop)

    def test_policy_readiness_update_and_published_version_are_separate(self):
        composition = self.compose()
        platform = composition.platform
        scope = platform.create_kb(self.actor, 'Synthetic document lifecycle')['kb_id']
        first = platform.ingest(self.actor, scope, 'Policy.md', b'Original rule value 42.')
        staged = platform.stage(self.actor, scope, ['bm25'])
        platform.publish(self.actor, scope, staged['build_id'])
        listing = platform.documents(self.actor, scope, provider_id='synthetic')
        document, = listing['documents']
        self.assertTrue(document['human_search_ready'])
        self.assertFalse(document['qa_ready'])
        self.assertIn('LLM_CONTEXT_DENIED', document['blockers'])
        platform.set_source_policy(self.actor, first['source_id'],
            {'llm_context': True, 'allowed_providers': ['synthetic']},
            expected_revision=listing['authority_revision'])
        current = platform.documents(self.actor, scope, provider_id='synthetic')
        self.assertTrue(current['documents'][0]['qa_ready'])
        self.assertFalse(current['documents'][0]['needs_publication'])
        second = platform.ingest(self.actor, scope, 'Policy.md', b'Original rule value 99.',
            source_id=first['source_id'], expected_revision=current['authority_revision'])
        listing = platform.documents(self.actor, scope, provider_id='synthetic')
        updated, = listing['documents']
        self.assertEqual(updated['source_version_id'], second['source_version_id'])
        self.assertEqual(updated['published_version_id'], first['source_version_id'])
        self.assertTrue(updated['needs_publication'])
        old = platform.search(self.actor, scope, 'Original rule')['hits']
        self.assertTrue(old)
        self.assertTrue(all('42' in hit['text'] for hit in old))
        versions = platform.source_versions(self.actor, scope, first['source_id'])
        self.assertEqual({item['source_version_id'] for item in versions['versions']},
            {first['source_version_id'], second['source_version_id']})
        staged = platform.stage(self.actor, scope, ['bm25'])
        platform.publish(self.actor, scope, staged['build_id'])
        current = platform.documents(self.actor, scope, provider_id='synthetic')
        self.assertFalse(current['documents'][0]['needs_publication'])
        hits = platform.search(self.actor, scope, 'Original rule')['hits']
        self.assertTrue(hits)
        self.assertTrue(all('99' in hit['text'] for hit in hits))
        platform.ingest(self.actor, scope, 'Policy.html', b'<p>Draft</p><img src="missing.png">',
            source_id=first['source_id'], expected_revision=current['authority_revision'])
        pending = platform.documents(self.actor, scope, provider_id='synthetic')['documents'][0]
        self.assertTrue(pending['qa_ready'])
        self.assertEqual(pending['blockers'], [])
        self.assertEqual(pending['build_blockers'], ['INCOMPLETE_PARSE'])
        self.assertEqual(pending['published_version_id'], second['source_version_id'])

    def test_partial_document_is_diagnosed_and_can_be_removed_without_hiding_failure(self):
        composition = self.compose()
        platform = composition.platform
        scope = platform.create_kb(self.actor, 'Synthetic incomplete parse')['kb_id']
        platform.ingest(self.actor, scope, 'Good.md', b'Complete original rule 42.')
        partial = platform.ingest(self.actor, scope, 'Incomplete.html',
            b'<p>Visible rule</p><img src="unavailable.png">')
        listing = platform.documents(self.actor, scope, provider_id='synthetic')
        document = next(item for item in listing['documents'] if item['source_id'] == partial['source_id'])
        self.assertIn(document['parse_status'], {'PARTIAL', 'BLOCKED'})
        self.assertIn('INCOMPLETE_PARSE', document['build_blockers'])
        reparsed = platform.reparse_source(self.actor, scope, partial['source_id'],
            expected_revision=listing['authority_revision'])
        self.assertEqual(reparsed['source_id'], partial['source_id'])
        self.assertEqual(reparsed['source_version_id'], partial['source_version_id'])
        listing = platform.documents(self.actor, scope, provider_id='synthetic')
        with self.assertRaises(PlatformError) as caught:
            platform.stage(self.actor, scope, ['bm25'])
        self.assertEqual(caught.exception.code, 'INCOMPLETE_PARSE')
        platform.remove_binding(self.actor, scope, document['binding_id'],
            expected_revision=listing['authority_revision'])
        self.assertEqual(platform.stage(self.actor, scope, ['bm25'])['status'], 'VALIDATED')
        removed = platform.documents(self.actor, scope, provider_id='synthetic', include_removed=True)
        self.assertEqual(next(item for item in removed['documents']
            if item['source_id'] == partial['source_id'])['binding_status'], 'REMOVED')

    def test_builtin_search_uses_published_parse_through_update_publish_and_revocation(self):
        from kb_platform.agent_builtin_tools import BuiltinTools
        from kb_platform.knowledge_contracts import EvidenceClient, EVIDENCE_METHODS

        composition = self.compose()
        platform = composition.platform
        scope = platform.create_kb(self.actor, 'Synthetic Agent version lifecycle')['kb_id']
        source = platform.ingest(self.actor, scope, 'Policy.md', b'QUARTZMARKER rule value 42.',
            policy={'llm_context': True, 'allowed_providers': ['synthetic']})
        staged = platform.stage(self.actor, scope, ['bm25'])
        platform.publish(self.actor, scope, staged['build_id'])
        contract = composition.application.composition
        gateway = EvidenceClient(SimpleNamespace(**{method:
            lambda request, method=method: contract.call('data', 'evidence', method, request)
            for method in EVIDENCE_METHODS}))

        def search():
            saved = []
            tool = BuiltinTools(**{field.name: None for field in fields(BuiltinTools)})
            tool.actor, tool.scope = self.actor, scope
            tool.state, tool.config, tool.trace = {'source_policy': {}, 'evidence': {}}, {}, {}
            tool.seen_turn_evidence, tool.max_active_evidence = set(), 5
            tool.turn_gateway, tool.gateway = gateway, gateway
            tool.retrieval_config = {'rerank': False, 'parent': 'none', 'title_navigation': False}
            tool.assessment_question = 'What is QUARTZMARKER?'
            tool.invoke_tool = lambda action, args, call, **options: call()
            tool.save_evidence = saved.extend
            result = tool.execute('search', {'query': 'QUARTZMARKER'}, 'synthetic-search')
            self.assertEqual(result['evidence_ids'], [item['citation_id'] for item in saved])
            self.assertEqual(tool.trace['retrieval_stages'][0]['config']['channels'], ['bm25'])
            return saved

        original = search()
        self.assertTrue(original)
        second = platform.ingest(self.actor, scope, 'Policy.md', b'QUARTZMARKER rule value 99.',
            source_id=source['source_id'])
        candidate = platform.documents(self.actor, scope, provider_id='synthetic')['documents'][0]
        self.assertTrue(candidate['qa_ready'])
        self.assertTrue(candidate['needs_publication'])
        retained = search()
        self.assertEqual([item['citation_id'] for item in retained], [item['citation_id'] for item in original])
        self.assertTrue(all(item['text'] == 'QUARTZMARKER rule value 42.' for item in retained))
        staged = platform.stage(self.actor, scope, ['bm25'])
        platform.publish(self.actor, scope, staged['build_id'])
        current = search()
        self.assertTrue(current)
        self.assertTrue(all(item['text'] == 'QUARTZMARKER rule value 99.' and
            item['source_version_id'] == second['source_version_id'] for item in current))
        platform.set_source_policy(self.actor, source['source_id'],
            {'llm_context': False, 'allowed_providers': ['synthetic']})
        self.assertEqual(search(), [])
        self.assertFalse(platform.documents(self.actor, scope, provider_id='synthetic')['documents'][0]['qa_ready'])
        self.assertEqual(self.physical, [])

    def test_reparse_after_mutable_authorization_update_preserves_immutable_parse(self):
        composition = self.compose()
        platform = composition.platform
        scope = platform.create_kb(self.actor, 'Synthetic reparse authorization')['kb_id']
        first = platform.ingest(self.actor, scope, 'Policy.md', b'Original rule value 42.')
        before = platform.documents(self.actor, scope)
        platform.set_source_policy(self.actor, first['source_id'],
            {'llm_context': True, 'allowed_providers': ['synthetic']},
            expected_revision=before['authority_revision'])
        current = platform.documents(self.actor, scope)
        reparsed = platform.reparse_source(self.actor, scope, first['source_id'],
            expected_revision=current['authority_revision'])
        self.assertEqual(reparsed['document']['parse_snapshot_id'], first['document']['parse_snapshot_id'])
        self.assertEqual(reparsed['document'], first['document'])
        self.assertEqual(set(reparsed['document']['processing_policy']),
            {'native_parse', 'ocr', 'include_hidden', 'include_notes'})
        self.assertTrue(platform.documents(self.actor, scope)['documents'][0]['policy']['llm_context'])
        self.assertEqual(self.physical, [])

    def test_stale_mutation_and_cross_identity_document_reads_fail_closed(self):
        composition = self.compose()
        platform = composition.platform
        scope = platform.create_kb(self.actor, 'Synthetic document permissions')['kb_id']
        source = platform.ingest(self.actor, scope, 'Rule.md', b'Rule 42.')
        before = platform.documents(self.actor, scope, provider_id='synthetic')
        policy = deepcopy(before['documents'][0]['policy'])
        platform.set_source_policy(self.actor, source['source_id'], {**policy, 'preview': False},
            expected_revision=before['authority_revision'])
        with self.assertRaises(PlatformError) as caught:
            platform.set_source_policy(self.actor, source['source_id'], policy,
                expected_revision=before['authority_revision'])
        self.assertEqual(caught.exception.code, 'AUTHORITY_REVISION_CONFLICT')
        for actor in (Actor(self.actor.tenant_id, 'other'), Actor('another-tenant', self.actor.principal_id)):
            with self.assertRaises(PlatformError):
                platform.documents(actor, scope, provider_id='synthetic')
            with self.assertRaises(PlatformError):
                platform.source_versions(actor, scope, source['source_id'])
        self.assertEqual(self.physical, [])
