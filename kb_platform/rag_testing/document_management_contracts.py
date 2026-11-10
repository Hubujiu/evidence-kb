from copy import deepcopy
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
