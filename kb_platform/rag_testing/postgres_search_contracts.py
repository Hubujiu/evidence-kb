from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.postgres_search import lexical_text, validate_vector, PostgresSearch
from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.search import OpenSearch


class PostgresSearchContracts(unittest.TestCase):
    def test_unbuilt_optional_semantic_index_does_not_block_knowledge_turn(self):
        from kb_platform.knowledge_service import KnowledgeService
        from kb_platform.semantic_pages_v2 import SemanticPageService

        backend = PostgresSearch.__new__(PostgresSearch)
        backend.pool = Mock()
        connection = Mock()
        backend.pool.connection.side_effect = lambda: nullcontext(connection)
        connection.execute.return_value.fetchone.return_value = None
        semantic = SemanticPageService.__new__(SemanticPageService)
        semantic.platform = SimpleNamespace(backend=backend)
        semantic.gateway = Mock()
        service = KnowledgeService.__new__(KnowledgeService)
        service.semantic_pages = semantic
        actor = Actor('tenant', 'user')
        self.assertFalse(service._semantic_available(actor, 'kb'))
        connection.execute.return_value.fetchone.return_value = {'manifest': {}, 'checksum': 'corrupt'}
        with self.assertRaises(PlatformError) as rejected:
            service._semantic_available(actor, 'kb')
        self.assertEqual(rejected.exception.code, 'SEMANTIC_PAGE_INDEX_UNAVAILABLE')
        self.assertIsInstance(rejected.exception.__cause__, ValueError)
        for code in ('FORBIDDEN', 'SEMANTIC_PAGE_INDEX_STALE', 'SEMANTIC_PAGE_MODEL_MISMATCH'):
            with self.subTest(code=code):
                semantic.gateway.authorize.side_effect = PlatformError(code, 'synthetic refusal', 409)
                with self.assertRaises(PlatformError) as refused:
                    service._semantic_available(actor, 'kb')
                self.assertEqual(refused.exception.code, code)

    def test_token_boundaries_survive_sql_parser(self):
        self.assertEqual(lexical_text('企业 知识'), lexical_text('企业，知识'))
        self.assertNotEqual(lexical_text('企业知识'), lexical_text('企业 知识'))
        self.assertEqual(2, len(lexical_text("don't foo_bar").split()))
        self.assertTrue(all(t.isalnum() for t in lexical_text("don't 中文").split()))

    def test_invalid_vectors_rejected(self):
        for vector in ([0, 0], [1], [float('nan'), 1], [True, 1], [float('inf'), 2]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                validate_vector(vector, 2)
        self.assertEqual('[1, 2]', validate_vector([1, 2], 2))

    def test_dynamic_identifiers_reject_injection(self):
        with self.assertRaises(ValueError):
            PostgresSearch._table('bad; DROP TABLE sources')

    def test_changed_search_dependencies_require_new_build(self):
        frozen = {'files': {'postgres_search.py': 'previous-code'}, 'packages': {'regex': '1'}}
        platform = Platform.__new__(Platform)
        platform.backend = object()
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {
            'status': 'STAGING', 'authority_revision': 1, 'kb_revision': 1,
            'expected_manifest_id': 'published', 'inputs': {'search_dependencies': frozen}}
        platform._tx = lambda *args: nullcontext(connection)
        platform._lock_authority = lambda *args: 1
        platform._auth = lambda *args: {'revision': 1, 'manifest_id': 'published'}
        for current in ({**frozen, 'files': {'postgres_search.py': 'current-code'}},
                        {**frozen, 'packages': {'regex': '2'}}):
            with self.subTest(current=current), patch('kb_platform.search.backend_dependencies', return_value=current):
                with self.assertRaises(PlatformError) as rejected:
                    platform.resume_build(Actor('tenant', 'user'), 'kb', 'build')
                self.assertEqual(rejected.exception.code, 'STALE_BUILD')

    def test_search_uses_current_binding_mapping_without_probe(self):
        backend = OpenSearch('https://search.invalid')
        backend._request = Mock(return_value={'hits': {'hits': []}})
        allowed = [{'binding_id': 'binding', 'source_id': 'source',
                    'source_version_id': 'version', 'parse_snapshot_id': 'parse'}]
        index = backend.index_name('tenant', 'kb', '1' * 32)
        self.assertEqual([], backend.search(index, 'tenant', 'kb', allowed, 'query', 5))
        backend._request.assert_called_once()
        args, kwargs = backend._request.call_args
        self.assertEqual(args, ('POST', f'/{index}/_search'))
        filters = kwargs['json']['query']['bool']['filter']
        self.assertEqual(filters[:2], [{'term': {'tenant_id': 'tenant'}}, {'term': {'kb_id': 'kb'}}])
        self.assertEqual(set(filters[2]['terms']), {'binding_key'})
