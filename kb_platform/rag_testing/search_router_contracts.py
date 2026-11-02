from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.platform_plugins import platform_plugins
from kb_platform.search import SearchUnavailable, backend_dependencies
from kb_platform.search_channels import SearchLexicalAdapter, SearchVectorAdapter
from kb_platform.search_evidence import DocumentEvidenceSource, unit_sha256
from kb_platform.search_router import channel_records
from kb_platform.search_router import SplitSearch
from kb_platform.storage_sdk import DOCUMENT_V1, LEXICAL_BM25_V1, VECTOR_V1, SpaceKey, StorageIdentity
from .product_fixture import Authority
from .storage_values_contracts import value_store


class SearchRouterContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.bm25 = EmbeddedSearch(self.root / 'lexical-database')
        self.dense = EmbeddedSearch(self.root / 'dense-database')
        self.addCleanup(self.bm25.close)
        self.addCleanup(self.dense.close)
        self.source = EmbeddedSearch(self.root / 'source-database')
        self.addCleanup(self.source.close)
        self.lexical = SearchLexicalAdapter(self.bm25)
        self.vector = SearchVectorAdapter(self.dense)
        self.router = SplitSearch(self.lexical, self.vector, self.source)
        self.authority = Authority([('public.md', 'Public enterprise permission rule.'),
                                    ('private.md', 'Private enterprise permission rule.')])
        self.units = self.authority.units
        self.units.append({**self.units[0], 'kind': 'Page', 'unit_id': 'page', 'citation_id': 'page'})
        for number, unit in enumerate(self.units):
            unit['vector'] = [1.0, float(number + 1), 0.0]
        self.index = self.router.index_name('tenant', 'scope', '1' * 32)

    def stage(self):
        self.router.stage(self.index, self.units, vector_dimension=3)

    def test_new_generation_not_found_is_distinct_from_partial_generation(self):
        with self.assertRaisesRegex(SearchUnavailable, '^Search generation not found$'):
            self.router.read_units(self.index)
        self.lexical.stage(self.router.channel_index(self.index, 'bm25'), channel_records(self.units, 'bm25'))
        with self.assertRaisesRegex(SearchUnavailable, 'Incomplete split search generation'):
            self.router.read_units(self.index)
        self.assertFalse(self.router.is_sealed(self.index, 'seal'))

    def test_two_real_stores_readback_and_restart_preserve_exact_records(self):
        self.stage()
        metadata = self.router.verify(self.index)
        self.assertEqual(metadata['count'], len(self.units))
        restarted = SplitSearch(SearchLexicalAdapter(EmbeddedSearch(self.bm25.root)),
                                SearchVectorAdapter(EmbeddedSearch(self.dense.root)), EmbeddedSearch(self.source.root))
        self.addCleanup(restarted.bm25.backend.close)
        self.addCleanup(restarted.vector.backend.close)
        self.addCleanup(restarted.source.close)
        self.assertEqual({unit['unit_id']: unit for unit in self.units},
                         {unit['unit_id']: unit for unit in restarted.read_units(self.index)})
        self.assertEqual(['page', self.units[0]['unit_id']],
                         [unit['unit_id'] for unit in restarted.read_units(self.index, ['page', self.units[0]['unit_id']])])

    def test_each_query_uses_its_configured_channel(self):
        self.stage()
        with patch.object(self.bm25, 'search', wraps=self.bm25.search) as lexical, \
                patch.object(self.dense, 'dense', wraps=self.dense.dense) as dense, \
                patch.object(self.lexical, 'query', wraps=self.lexical.query) as lexical_query, \
                patch.object(self.vector, 'query', wraps=self.vector.query) as dense_query, \
                patch.object(self.dense, 'search', side_effect=AssertionError('wrong lexical route')), \
                patch.object(self.bm25, 'dense', side_effect=AssertionError('wrong dense route')):
            self.assertTrue(self.router.search(self.index, 'tenant', 'scope', self.authority.rows, 'permission', 5))
            self.assertTrue(self.router.dense(self.index, 'tenant', 'scope', self.authority.rows, [1, 1, 0], 5))
            self.assertTrue(lexical.called)
            self.assertTrue(dense.called)
            self.assertEqual(lexical_query.call_args.args[0], self.router.channel_index(self.index, 'bm25'))
            self.assertEqual(dense_query.call_args.args[0], self.router.channel_index(self.index, 'dense'))

    def test_current_permission_and_scope_are_enforced_by_both_channels(self):
        self.stage()
        for method, query in (('search', 'permission'), ('dense', [1, 1, 0])):
            search = getattr(self.router, method)
            with self.subTest(method=method):
                hits = search(self.index, 'tenant', 'scope', self.authority.rows[1:], query, 1)
                self.assertEqual({'s1'}, {hit['source_id'] for hit in hits})
                self.assertEqual([], search(self.index, 'other', 'scope', self.authority.rows, query, 5))
                self.assertEqual([], search(self.index, 'tenant', 'scope', [], query, 5))
                revoked = [{**self.authority.rows[0], 'parse_snapshot_id': 'revoked-parse'}]
                self.assertEqual([], search(self.index, 'tenant', 'scope', revoked, query, 5))

    def test_shared_physical_store_uses_distinct_immutable_channel_generations(self):
        router = SplitSearch(self.lexical, SearchVectorAdapter(self.bm25), self.source)
        router.stage(self.index, self.units, 3)
        self.assertNotEqual(router.channel_index(self.index, 'bm25'), router.channel_index(self.index, 'dense'))
        self.assertEqual(len(self.units), len(router.read_units(self.index)))
        router.seal(self.index, 'sealed')
        self.assertTrue(router.is_sealed(self.index, 'sealed'))

    def test_page_channel_exclusions_and_expansion_use_same_canonical_records(self):
        self.stage()
        allowed = self.authority.rows[:1]
        pages = self.router.dense_pages(self.index, 'tenant', 'scope', self.units[0]['build_id'],
                                        allowed, [1, 1, 0], 5, ())
        self.assertEqual(['page'], [page['citation_id'] for page in pages])
        self.assertEqual([], self.router.dense_pages(self.index, 'tenant', 'scope', self.units[0]['build_id'],
                                                    allowed, [1, 1, 0], 5, ['page']))
        hit = self.router.search(self.index, 'tenant', 'scope', allowed, 'permission', 1)[0]
        self.assertTrue(self.router.expand(self.index, 'tenant', 'scope', hit))

    def test_mismatched_canonical_content_prevents_query_and_seal(self):
        target = self.router.channel_index(self.index, 'bm25')
        self.source.stage(self.index, self.units, 3)
        self.lexical.stage(target, channel_records(self.units, 'bm25'))
        changed = [{**unit, 'text': unit['text'] + ' changed'} for unit in self.units]
        self.vector.stage(self.router.channel_index(self.index, 'dense'), channel_records(changed, 'dense'), 3)
        with self.assertRaisesRegex(SearchUnavailable, 'canonical content mismatch'):
            self.router.seal(self.index, 'seal')
        with self.assertRaisesRegex(SearchUnavailable, 'canonical content mismatch'):
            self.router.search(self.index, 'tenant', 'scope', self.authority.rows, 'permission', 5)
        self.assertFalse(self.lexical.is_sealed(target, 'seal'))

    def test_second_store_failure_never_reaches_manifest_publication(self):
        pointer = Mock()
        self.source.supports_manifest_storage = True
        self.source.publish_manifest = pointer
        router = SplitSearch(self.lexical, self.vector, self.source)
        with patch.object(self.dense, 'stage', side_effect=SearchUnavailable('synthetic dense outage')):
            with self.assertRaisesRegex(SearchUnavailable, 'synthetic dense outage'):
                router.stage(self.index, self.units, 3)
        with self.assertRaises(SearchUnavailable):
            router.publish_manifest({'index_name': self.index}, 'seal')
        pointer.assert_not_called()

    def test_retry_after_second_store_failure_preserves_first_store_and_completes_pair(self):
        with patch.object(self.dense, 'stage', side_effect=SearchUnavailable('dense outage')):
            with self.assertRaisesRegex(SearchUnavailable, 'dense outage'):
                self.stage()
        with patch.object(self.bm25, 'stage', side_effect=AssertionError('must preserve completed channel')):
            self.stage()
            self.stage()
        self.assertEqual(len(self.units), self.router.verify(self.index)['count'])
        self.router.seal(self.index, 'recovered')
        self.assertTrue(self.router.is_sealed(self.index, 'recovered'))

    def test_retry_refuses_to_replace_completed_channel_with_different_content(self):
        self.lexical.stage(self.router.channel_index(self.index, 'bm25'), channel_records(self.units, 'bm25'))
        changed = [{**unit, 'text': unit['text'] + ' changed'} for unit in self.units]
        with self.assertRaisesRegex(SearchUnavailable, 'differs from frozen input'):
            self.router.stage(self.index, changed, 3)
        with self.assertRaisesRegex(SearchUnavailable, '^Search generation not found$'):
            self.vector.verify(self.router.channel_index(self.index, 'dense'))

    def test_provider_identity_and_channel_order_are_in_dependency_hash(self):
        first = backend_dependencies(self.router)
        swapped = backend_dependencies(SplitSearch(SearchLexicalAdapter(self.dense), SearchVectorAdapter(self.bm25), self.source))
        self.assertNotEqual(first, swapped)
        space = SpaceKey('independent', 'products', 'vectors')
        first_dataset = SearchVectorAdapter(self.dense, identity=StorageIdentity(space, 'dataset-a'))
        second_dataset = SearchVectorAdapter(self.dense, identity=StorageIdentity(space, 'dataset-b'))
        self.assertNotEqual(backend_dependencies(first_dataset), backend_dependencies(second_dataset))
        self.assertNotIn(str(self.root), str(backend_dependencies(first_dataset)))

    def test_channel_contracts_do_not_require_document_or_other_search_interfaces(self):
        VECTOR_V1.validate(self.vector)
        LEXICAL_BM25_V1.validate(self.lexical)
        for service in (self.vector, self.lexical):
            for method in ('read_units', 'search', 'dense', 'expand', 'get', 'put'):
                self.assertFalse(hasattr(service, method), method)
        self.stage()
        vectors = self.vector.read_records(self.router.channel_index(self.index, 'dense'))
        lexical = self.lexical.read_records(self.router.channel_index(self.index, 'bm25'))
        self.assertTrue(all(set(record) == {'id', 'metadata', 'vector'} for record in vectors))
        self.assertTrue(all('vector' not in record and 'text' in record for record in lexical))
        self.assertTrue(all(not ({'text', 'anchors', 'evidence_ids'} & set(record['metadata'])) for record in vectors))
        hit = self.router.dense(self.index, 'tenant', 'scope', self.authority.rows, [1, 1, 0], 1)[0]
        canonical = next(unit for unit in self.units if unit['unit_id'] == hit['unit_id'])
        self.assertEqual(hit['text'], canonical['text'])
        self.assertEqual(hit['anchors'], canonical['anchors'])

    def test_generic_vector_contract_filters_before_nearest_neighbour_ranking(self):
        index = 'products-v1'
        records = [{'id': 'private', 'vector': [1, 0, 0], 'metadata': {'group': 'private'}},
                   {'id': 'public', 'vector': [0, 1, 0], 'metadata': {'group': 'public'}}]
        self.vector.stage(index, records, 3)
        hits = self.vector.query(index, [1, 0, 0], 1, filters={'eq': {'group': 'public'}})
        self.assertEqual([hit['id'] for hit in hits], ['public'])
        self.assertEqual(self.vector.read_records(index), records)
        with self.assertRaisesRegex(ValueError, 'filters'):
            self.vector.query(index, [1, 0, 0], 1, filters={'unknown': {}})

    def test_independent_named_collections_restart_and_isolate_contract_namespaces(self):
        backend = EmbeddedSearch(self.root / 'independent-plugin')
        self.addCleanup(backend.close)
        vector, lexical = SearchVectorAdapter(backend), SearchLexicalAdapter(backend)
        vector.stage('products-v1', [{'id': 'p1', 'vector': [1, 0, 0], 'metadata': {'category': 'public'}}], 3)
        lexical.stage('products-v1', [{'id': 'p2', 'text': 'enterprise widget', 'metadata': {'category': 'public'}}])
        vector.stage('products-v2', [{'id': 'p3', 'vector': [0, 1, 0], 'metadata': {}}], 3)
        self.assertEqual([hit['id'] for hit in vector.query('products-v1', [1, 0, 0], 2)], ['p1'])
        self.assertEqual([hit['id'] for hit in lexical.query('products-v1', 'widget', 2)], ['p2'])
        self.assertEqual([hit['id'] for hit in vector.query('products-v2', [1, 0, 0], 2)], ['p3'])
        reopened = EmbeddedSearch(backend.root)
        self.addCleanup(reopened.close)
        self.assertEqual([hit['id'] for hit in SearchVectorAdapter(reopened).query('products-v1', [1, 0, 0], 2)], ['p1'])
        self.assertEqual([hit['id'] for hit in SearchLexicalAdapter(reopened).query('products-v1', 'widget', 2)], ['p2'])
        for name in ('', '../escape', 'x/y', 'x\x00y', 'x' * 129):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'collection name'):
                vector.stage(name, [{'id': 'invalid', 'vector': [1, 0, 0], 'metadata': {}}], 3)

    def test_document_authority_restart_publication_and_current_acl(self):
        root = self.root / 'document-authority'
        with value_store(root) as documents:
            identity = documents.identity.dataset_id
            router = SplitSearch(self.lexical, self.vector, DocumentEvidenceSource(documents))
            router.stage(self.index, self.units, 3)
            router.seal(self.index, 'synthetic-checksum')
            manifest = {'index_name': self.index, 'tenant_id': 'tenant', 'kb_id': 'scope'}
            router.publish_manifest(manifest, 'synthetic-checksum')
            self.assertEqual(router.current_manifest('tenant', 'scope'), (manifest, 'synthetic-checksum'))
        with value_store(root, dataset=identity) as documents:
            restarted = SplitSearch(self.lexical, self.vector, DocumentEvidenceSource(documents))
            self.assertTrue(restarted.is_sealed(self.index, 'synthetic-checksum'))
            self.assertEqual(restarted.current_manifest('tenant', 'scope'), (manifest, 'synthetic-checksum'))
            self.assertEqual(restarted.read_units(self.index), sorted(self.units, key=lambda unit: (
                unit['source_version_id'], unit['parse_snapshot_id'], unit['ordinal'], unit['unit_id'])))
            hits = restarted.dense(self.index, 'tenant', 'scope', self.authority.rows[1:], [1, 1, 0], 5)
            self.assertEqual({hit['source_id'] for hit in hits}, {'s1'})

    def test_document_authority_mutation_is_detected_before_evidence_delivery(self):
        with value_store(self.root / 'documents') as documents:
            source = DocumentEvidenceSource(documents)
            router = SplitSearch(self.lexical, self.vector, source)
            router.stage(self.index, self.units, 3)
            router.seal(self.index, 'sealed')
            with self.assertRaisesRegex(SearchUnavailable, 'Sealed evidence'):
                source.stage(self.index, self.units, 3)
            key = source._key(self.index, unit_sha256(self.units[0]))
            record = documents.get(key)
            documents.put(key, record.value | {'text': 'corrupted authoritative evidence'}, expected_revision=record.revision)
            with self.assertRaisesRegex(SearchUnavailable, 'integrity mismatch'):
                router.dense(self.index, 'tenant', 'scope', self.authority.rows, [1, 1, 0], 5)
            self.assertFalse(router.is_sealed(self.index, 'sealed'))

    def test_half_seal_is_not_published_and_retry_completes_without_replacing_channels(self):
        with value_store(self.root / 'documents') as documents:
            source = DocumentEvidenceSource(documents)
            router = SplitSearch(self.lexical, self.vector, source)
            router.stage(self.index, self.units, 3)
            with patch.object(self.vector, 'seal', side_effect=SearchUnavailable('synthetic seal outage')):
                with self.assertRaisesRegex(SearchUnavailable, 'seal outage'):
                    router.seal(self.index, 'sealed')
            manifest = {'index_name': self.index, 'tenant_id': 'tenant', 'kb_id': 'scope'}
            with self.assertRaisesRegex(SearchUnavailable, 'must be sealed'):
                router.publish_manifest(manifest, 'sealed')
            with self.assertRaises(FileNotFoundError):
                source.current_manifest('tenant', 'scope')
            with patch.object(self.lexical, 'seal', side_effect=AssertionError('preserve sealed lexical generation')):
                router.seal(self.index, 'sealed')
            router.publish_manifest(manifest, 'sealed')
            self.assertEqual(router.current_manifest('tenant', 'scope'), (manifest, 'sealed'))

    def test_seal_binds_actual_dataset_identity_not_only_generation_content(self):
        with value_store(self.root / 'documents') as documents:
            router = SplitSearch(self.lexical, self.vector, DocumentEvidenceSource(documents))
            router.stage(self.index, self.units, 3)
            router.seal(self.index, 'sealed')
            original = self.vector.identity
            self.vector.identity = 'different-dataset'
            try:
                self.assertFalse(router.is_sealed(self.index, 'sealed'))
                with self.assertRaisesRegex(SearchUnavailable, 'must be sealed'):
                    router.publish_manifest({'index_name': self.index}, 'sealed')
            finally:
                self.vector.identity = original
            self.assertTrue(router.is_sealed(self.index, 'sealed'))

    def test_search_plugin_declares_only_channel_capabilities_and_authoritative_documents(self):
        specs = {spec.key: spec for spec in platform_plugins(SimpleNamespace(storage_root=self.root))}
        router = specs['platform.search.storage']
        self.assertEqual(3, len(router.requires))
        self.assertEqual(('platform.search',), router.provides)
        self.assertEqual({'bm25': LEXICAL_BM25_V1, 'dense': VECTOR_V1, 'source': DOCUMENT_V1},
                         {slot.name: slot.contract for slot in router.requires})


if __name__ == '__main__':
    unittest.main()
