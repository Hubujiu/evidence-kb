from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest

from kb_platform.database_contracts import QueryResult, SpaceKey, StorageIdentity
from kb_platform.search import SearchUnavailable
from kb_platform.storage_adapters.postgres_search import PostgresSearchStore
from kb_platform.storage_sdk import StorageError


class _Database:
    def __init__(self, extensions):
        self.extensions = set(extensions)
        self.info = SimpleNamespace(engine='postgresql')
        self.identity = StorageIdentity(SpaceKey('ordinary-app', 'products', 'search'), 'stable-dataset')
        self.physical_name = 'ordinary_search'
        self.packages, self.calls = [], []
        self.sealed = False

    def prepare(self, package):
        if self.sealed:
            raise RuntimeError('prepare after seal')
        self.packages.append(package)

    def seal(self):
        self.sealed = True

    @contextmanager
    def transaction(self, *, write=False, limits=None):
        if not self.sealed:
            raise RuntimeError('query before schema preparation')
        yield self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if 'FROM pg_extension' in sql:
            extension = params['extension']
            return QueryResult([{'extversion': 'fixture', 'nspname': 'public'}]
                               if extension in self.extensions else [])
        return QueryResult()


class GenericPostgresSearchContracts(unittest.TestCase):
    def test_vector_export_only_requires_its_own_extension(self):
        database = _Database({'vector'})
        selected = PostgresSearchStore(database, 'vector')
        self.assertEqual(selected.identity, database.identity)
        self.assertEqual([params['extension'] for _, params in database.calls], ['vector'])

    def test_lexical_export_only_requires_its_own_extension(self):
        database = _Database({'pg_textsearch'})
        PostgresSearchStore(database, 'lexical')
        self.assertEqual([params['extension'] for _, params in database.calls], ['pg_textsearch'])

    def test_missing_required_extension_refuses_before_generation_write(self):
        for channel, installed, missing in (('vector', {'pg_textsearch'}, 'vector'),
                                             ('lexical', {'vector'}, 'pg_textsearch')):
            with self.subTest(channel=channel):
                database = _Database(installed)
                with self.assertRaises(StorageError) as raised:
                    PostgresSearchStore(database, channel)
                self.assertEqual(raised.exception.code, 'STORAGE_EXTENSION_REQUIRED')
                self.assertEqual(raised.exception.details['extension'], missing)
                self.assertTrue(all('storage_search_build' not in sql for sql, _ in database.calls))

    def test_arbitrary_vector_records_remain_generic_records(self):
        database = _Database({'vector'})
        selected = PostgresSearchStore(database, 'vector')
        record = {'id': 'product-01', 'metadata': {'department': 'furniture', 'price': 12.5}, 'vector': [1, 0, 0]}
        selected.stage('products-v1', [record], vector_dimension=3)
        _, params = database.calls[-1]
        self.assertEqual(json.loads(params['records']), [{'record': record}])
        self.assertEqual(params['channel'], 'vector')
        self.assertEqual(selected.storage_identity()['dataset'], str(database.identity))

    def test_arbitrary_lexical_records_keep_text_and_metadata(self):
        database = _Database({'pg_textsearch'})
        selected = PostgresSearchStore(database, 'lexical')
        record = {'id': 'product-02', 'metadata': {'catalog': ['office']},
                  'text': 'Office supplies require review.', 'heading': 'Procurement'}
        selected.stage('采购清单', [record])
        payload = json.loads(database.calls[-1][1]['records'])
        self.assertEqual(payload[0]['record'], record)
        self.assertTrue(payload[0]['body'])
        self.assertTrue(payload[0]['exact'])
        self.assertTrue(payload[0]['heading'])
        self.assertEqual(set(payload[0]), {'record', 'body', 'exact', 'heading'})

    def test_invalid_records_are_rejected_before_mutation(self):
        invalid = [[], [{'id': 'duplicate', 'metadata': {}}, {'id': 'duplicate', 'metadata': {}}],
                   [{'id': 'wrong-channel', 'metadata': {}, 'text': 'document'}],
                   [{'id': 'invalid-vector', 'metadata': {}, 'vector': [1, float('nan'), 0]}],
                   [{'id': 'overflow-vector', 'metadata': {}, 'vector': [1e100, 0, 0]}],
                   [{'id': 'empty-vector', 'metadata': {}, 'vector': [0, 0, 0]}],
                   [{'id': 'no-metadata', 'vector': [1, 0, 0]}]]
        for records in invalid:
            with self.subTest(records=records):
                database = _Database({'vector'})
                selected = PostgresSearchStore(database, 'vector')
                before = list(database.calls)
                with self.assertRaises(SearchUnavailable):
                    selected.stage('ordinary-products', records, vector_dimension=3)
                self.assertEqual(database.calls, before)

    def test_invalid_collection_is_rejected_before_mutation(self):
        database = _Database({'vector'})
        selected = PostgresSearchStore(database, 'vector')
        before = list(database.calls)
        with self.assertRaises(ValueError):
            selected.stage('unsafe; DROP TABLE products', [{'id': 'p', 'metadata': {}, 'vector': [1]}], 1)
        self.assertEqual(database.calls, before)

    def test_generation_without_vectors_remains_available_for_optional_embedding(self):
        database = _Database({'vector'})
        selected = PostgresSearchStore(database, 'vector')
        selected.stage('embedding-disabled', [{'id': 'product', 'metadata': {'category': 'office'}}])
        params = database.calls[-1][1]
        self.assertIsNone(params['dimension'])
        self.assertEqual(json.loads(params['records'])[0]['record'],
                         {'id': 'product', 'metadata': {'category': 'office'}})
