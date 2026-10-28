"""Offline PageIndex adapter boundary contracts; no provider calls."""
import unittest

from .contracts import Source, text_hash
from .pageindex_probe import build_index, choose_ids, pack


class PageIndexContracts(unittest.TestCase):
    def setUp(self):
        text = '# A\nintro\n## B\nvalue\n```\n# fake heading\n```\n'
        self.source = Source(id='allowed', kb='k', version='v', text=text, sha256=text_hash(text))
        self.index, self.fragments = build_index((self.source,))

    def test_headings_preserve_original_text_and_ignore_code_fences(self):
        self.assertEqual(len(self.fragments), 2)
        self.assertEqual(''.join(f.text for f in self.fragments.values()), self.source.text)
        self.assertEqual(len(self.index['directory'][0]['nodes'][0]['nodes']), 1)

    def test_invalid_unauthorized_and_duplicate_node_ids_are_rejected(self):
        for value in ({'node_ids': ['forbidden:0001']}, {'node_ids': ['allowed:0001'] * 2},
                      {'node_ids': 'bad'}, {'node_ids': [None]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                choose_ids(value, self.fragments)

    def test_insufficient_budget_drops_whole_fragments(self):
        selected, context, dropped = pack(tuple(self.fragments.values()), 2)
        self.assertFalse(selected)
        self.assertEqual(context, '[]')
        self.assertEqual(len(dropped), 2)

    def test_repeated_fragments_are_packed_once(self):
        selected, _, dropped = pack(tuple(self.fragments.values()) * 2, 16000)
        self.assertEqual(len(selected), 2)
        self.assertFalse(dropped)

    def test_preamble_is_preserved(self):
        text = 'preamble\n## Child\nbody'
        source = self.source.model_copy(update={'text': text, 'sha256': text_hash(text)})
        _, fragments = build_index((source,))
        self.assertEqual(''.join(f.text for f in fragments.values()), text)

    def test_revoked_corpus_has_no_addressable_nodes(self):
        _, fragments = build_index(())
        self.assertFalse(fragments)
        with self.assertRaises(ValueError):
            choose_ids({'node_ids': ['allowed:0001']}, fragments)


if __name__ == '__main__':
    unittest.main()
