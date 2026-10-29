"""Offline input contracts; database behavior is exercised by live acceptance."""
import unittest

from kb_platform.postgres_search import lexical_text, validate_vector, PostgresSearch


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
