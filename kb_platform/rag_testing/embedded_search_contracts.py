from pathlib import Path
import tempfile
import unittest

from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.search import SearchUnavailable
from kb_platform.search_tokens import tokens
from .product_fixture import Authority


class EmbeddedSearchContracts(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.backend = EmbeddedSearch(self.folder.name)
        self.addCleanup(self.backend.close)
        self.authority = Authority([('one.md', '# 权限\n\n企业知识库权限收紧后不可访问。'),
                                    ('two.md', '# 权限\n\n企业知识库允许访问公开文档。')])
        self.index = self.backend.index_name('tenant', 'scope', '1' * 32)
        self.authority.units.append({**self.authority.units[0], 'kind': 'Page', 'unit_id': 'page', 'citation_id': 'page'})
        for i, unit in enumerate(self.authority.units):
            unit['vector'] = [1.0, float(i + 1), 0.0]
        self.backend.stage(self.index, self.authority.units, 3)

    def test_restart_and_revoke(self):
        backend = EmbeddedSearch(self.folder.name)
        for channel in ('search', 'dense'):
            query = '企业知识库权限' if channel == 'search' else [1.0, 1.0, 0.0]
            search = getattr(backend, channel)
            hits = search(self.index, 'tenant', 'scope', self.authority.rows[:1], query, 20)
            self.assertTrue(hits)
            self.assertEqual({'s0'}, {h['source_id'] for h in hits})
            self.assertEqual([], search(self.index, 'tenant', 'scope', [], query, 20))
            self.assertEqual([], search(self.index, 'another-tenant', 'scope', self.authority.rows, query, 20))

    def test_exact_vectors_and_seal(self):
        original = {u['unit_id']: u for u in self.authority.units}
        self.assertEqual(original, {u['unit_id']: u for u in self.backend.read_units(self.index)})
        self.backend.seal(self.index, 'manifest-hash')
        self.assertTrue(self.backend.is_sealed(self.index, 'manifest-hash'))
        self.assertFalse(self.backend.is_sealed(self.index, 'other-hash'))
        with self.assertRaises(SearchUnavailable):
            self.backend.stage(self.index, self.authority.units, 3)
        path = Path(self.folder.name) / self.index / 'vectors.npy'
        with path.open('ab') as stream:
            stream.write(b'corrupt')
        self.assertFalse(self.backend.is_sealed(self.index, 'manifest-hash'))

    def test_page_channel_and_exclusion(self):
        pages = self.backend.search(self.index, 'tenant', 'scope', self.authority.rows, '权限', 20, kind='Page')
        self.assertTrue(pages)
        self.assertEqual({'Page'}, {p['kind'] for p in pages})
        excluded = [p['citation_id'] for p in pages]
        self.assertEqual([], self.backend.dense_pages(self.index, 'tenant', 'scope', 'build',
            self.authority.rows, [1, 1, 0], 20, excluded))

    def test_failed_staging_not_visible(self):
        index = self.backend.index_name('tenant', 'scope', '2' * 32)
        with self.assertRaises(SearchUnavailable):
            self.backend.stage(index, [{**self.authority.units[0], 'vector': [float('inf'), 0, 0]}], 3)
        self.assertFalse((Path(self.folder.name) / index).exists())

    def test_cjk_boundaries(self):
        self.assertEqual(['北京', '上海', '北京'], tokens('北京 上海，北京'))
        self.assertEqual(['abc', '123', 'foo_bar', '3.14', "don't", '中文'], tokens("ABC-123 foo_bar 3.14 don't 中文"))
        self.assertEqual(['企', '业'], tokens('企业', exact=True))

    def test_pages_do_not_change_block_scores(self):
        index = self.backend.index_name('tenant', 'scope', '3' * 32)
        self.backend.stage(index, [u for u in self.authority.units if u['kind'] != 'Page'], 3)
        args = ('tenant', 'scope', self.authority.rows, '企业知识库权限', 20)
        before = self.backend.search(index, *args)
        after = self.backend.search(self.index, *args)
        self.assertEqual([(h['unit_id'], h['score']) for h in before], [(h['unit_id'], h['score']) for h in after])

    def test_old_binding_revision_cannot_match(self):
        allowed = [{**self.authority.rows[0], 'parse_snapshot_id': 'new-parse'}]
        self.assertEqual([], self.backend.search(self.index, 'tenant', 'scope', allowed, '权限', 20))
        self.assertEqual([], self.backend.dense(self.index, 'tenant', 'scope', allowed, [1, 1, 0], 20))

    def test_restricted_top_one_is_not_filtered_after_global_ranking(self):
        unrestricted = self.backend.dense(self.index, 'tenant', 'scope', self.authority.rows, [1, 0, 0], 1)
        self.assertEqual('s0', unrestricted[0]['source_id'])
        restricted = self.backend.dense(self.index, 'tenant', 'scope', self.authority.rows[1:], [1, 0, 0], 1)
        self.assertEqual(1, len(restricted))
        self.assertEqual('s1', restricted[0]['source_id'])


if __name__ == '__main__':
    unittest.main()
