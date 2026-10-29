"""Offline input contracts; database behavior is exercised by live acceptance."""
import unittest

from kb_platform.postgres_search import lexical_text, validate_vector, PostgresSearch
from kb_platform.search_compat import resumable_search_dependencies


class PostgresSearchContracts(unittest.TestCase):
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

    def test_only_reviewed_query_fix_can_resume_frozen_generation(self):
        before = {'files': {'postgres_search.py': 'ce616199541d736792c7cf8e1d8b73b439eca5eaeeb868c00e495b5c5a745ae8',
                            'search_tokens.py': 'frozen'}, 'packages': {'regex': '1'}}
        after = {'files': {**before['files'], 'postgres_search.py':
                 '02cf199bc551b9986363d96ddb5a7da9bd009f010485d30a1b549d1f5bc3e31b'}, 'packages': {'regex': '1'}}
        self.assertTrue(resumable_search_dependencies(before, after))
        self.assertFalse(resumable_search_dependencies(before, {**after, 'packages': {'regex': '2'}}))
        self.assertFalse(resumable_search_dependencies(before, {**after, 'files': {**after['files'], 'postgres_search.py': 'other'}}))
