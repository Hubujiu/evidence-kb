from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.platform_plugins import platform_plugins
from kb_platform.postgres_search import PostgresSearch
from kb_platform.search import SearchUnavailable, backend_dependencies
from kb_platform.search_router import SplitSearch
from .product_fixture import Authority


class SearchRouterContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.bm25 = EmbeddedSearch(self.root / 'lexical-database')
        self.dense = EmbeddedSearch(self.root / 'dense-database')
        self.addCleanup(self.bm25.close)
        self.addCleanup(self.dense.close)
        self.router = SplitSearch(self.bm25, self.dense)
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
        self.bm25.stage(self.router.channel_index(self.index, 'bm25'), self.units, 3)
        with self.assertRaisesRegex(SearchUnavailable, 'Incomplete split search generation'):
            self.router.read_units(self.index)
        self.assertFalse(self.router.is_sealed(self.index, 'seal'))

    def test_two_real_stores_readback_and_restart_preserve_exact_records(self):
        self.stage()
        metadata = self.router.verify(self.index)
        self.assertEqual(metadata['count'], len(self.units))
        restarted = SplitSearch(EmbeddedSearch(self.bm25.root), EmbeddedSearch(self.dense.root))
        self.addCleanup(restarted.bm25.close)
        self.addCleanup(restarted.vector.close)
        self.assertEqual({unit['unit_id']: unit for unit in self.units},
                         {unit['unit_id']: unit for unit in restarted.read_units(self.index)})
        self.assertEqual(['page', self.units[0]['unit_id']],
                         [unit['unit_id'] for unit in restarted.read_units(self.index, ['page', self.units[0]['unit_id']])])

    def test_each_query_uses_its_configured_channel(self):
        self.stage()
        with patch.object(self.bm25, 'search', wraps=self.bm25.search) as lexical, \
                patch.object(self.dense, 'dense', wraps=self.dense.dense) as dense, \
                patch.object(self.dense, 'search', side_effect=AssertionError('wrong lexical route')), \
                patch.object(self.bm25, 'dense', side_effect=AssertionError('wrong dense route')):
            self.assertTrue(self.router.search(self.index, 'tenant', 'scope', self.authority.rows, 'permission', 5))
            self.assertTrue(self.router.dense(self.index, 'tenant', 'scope', self.authority.rows, [1, 1, 0], 5))
            self.assertEqual(lexical.call_args.args[0], self.router.channel_index(self.index, 'bm25'))
            self.assertEqual(dense.call_args.args[0], self.router.channel_index(self.index, 'dense'))

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
        router = SplitSearch(self.bm25, self.bm25)
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
        self.bm25.stage(target, self.units, 3)
        changed = [{**unit, 'text': unit['text'] + ' changed'} for unit in self.units]
        self.dense.stage(self.router.channel_index(self.index, 'dense'), changed, 3)
        with self.assertRaisesRegex(SearchUnavailable, 'canonical content mismatch'):
            self.router.seal(self.index, 'seal')
        with self.assertRaisesRegex(SearchUnavailable, 'canonical content mismatch'):
            self.router.search(self.index, 'tenant', 'scope', self.authority.rows, 'permission', 5)
        self.assertFalse(self.bm25.is_sealed(target, 'seal'))

    def test_second_store_failure_never_reaches_manifest_publication(self):
        pointer = Mock()
        self.bm25.supports_manifest_storage = True
        self.bm25.publish_manifest = pointer
        router = SplitSearch(self.bm25, self.dense)
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
        self.bm25.stage(self.router.channel_index(self.index, 'bm25'), self.units, 3)
        changed = [{**unit, 'text': unit['text'] + ' changed'} for unit in self.units]
        with self.assertRaisesRegex(SearchUnavailable, 'differs from frozen input'):
            self.router.stage(self.index, changed, 3)
        with self.assertRaisesRegex(SearchUnavailable, '^Search generation not found$'):
            self.dense.verify(self.router.channel_index(self.index, 'dense'))

    def test_provider_identity_and_channel_order_are_in_dependency_hash(self):
        first = backend_dependencies(self.router)
        swapped = backend_dependencies(SplitSearch(self.dense, self.bm25))
        self.assertNotEqual(first, swapped)
        postgres_a = PostgresSearch('postgresql://localhost/database_a')
        postgres_b = PostgresSearch('postgresql://localhost/database_b')
        self.addCleanup(postgres_a.close)
        self.addCleanup(postgres_b.close)
        self.assertNotEqual(backend_dependencies(postgres_a), backend_dependencies(postgres_b))
        self.assertNotIn('postgresql://', str(backend_dependencies(postgres_a)))

    def test_split_plugin_declares_both_channel_dependencies(self):
        specs = {spec.key: spec for spec in platform_plugins(SimpleNamespace(storage_root=self.root, dsn=''))}
        router = specs['platform.search.split']
        self.assertEqual(('platform.search.bm25', 'platform.search.dense'), router.requires)
        self.assertEqual(('platform.search',), router.provides)
        for channel in ('bm25', 'dense'):
            for backend in ('postgres', 'embedded'):
                self.assertEqual(('platform.search.' + channel,), specs[f'platform.search.{channel}.{backend}'].provides)


if __name__ == '__main__':
    unittest.main()
