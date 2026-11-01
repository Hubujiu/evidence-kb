from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.database_contracts import Migration, SchemaPackage
from kb_platform.database_providers import SQLiteProvider
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_adapters.values import SqlDocumentStore, SqlKeyValueStore
from kb_platform.storage_process import MAX_REQUEST_BYTES, MAX_RESULT_BYTES, decode_value, encode_value
from kb_platform.storage_sdk import (
    DOCUMENT_V1, Document, ExistingDataset, KV_DURABLE_V1, RELATIONAL_V1, SpaceKey, StorageError,
)
from kb_platform.storage_service import StorageService


def _request(method, *args, **kwargs):
    return {'method': method, 'args': [encode_value(value) for value in args],
            'kwargs': {key: encode_value(value) for key, value in kwargs.items()}}


class StorageProcessContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.provider = SqlStorageProvider(SQLiteProvider(Path(temporary.name)), adapters={
            'document': ((DOCUMENT_V1,), SqlDocumentStore), 'kv': ((KV_DURABLE_V1,), SqlKeyValueStore)})
        self.addCleanup(self.provider.close)
        self.registry = StorageService()
        self.addCleanup(self.registry.close)
        self.exports = {export.name: export for export in self.provider.exports()}

    def bound(self, name, contract):
        key = SpaceKey('process', 'records', name)
        identity = self.provider.initialize(name, key)
        self.registry.register(name, self.exports[name])
        grant = self.registry.grant(name, contract, key, 'consumer', target=ExistingDataset(identity.dataset_id))
        return self.registry.bind(grant)

    def test_codec_preserves_documents_binary_and_json_that_looks_like_wire_tags(self):
        value = {'record': Document('one', {'type': 'bytes', 'value': 'literal JSON'}, 2),
                 'bytes': b'\x00\xff', 'values': [None, True, 12, 1.5, '正文']}
        self.assertEqual(decode_value(encode_value(value)), value)
        for invalid in ({'type': 'bytes', 'value': '?'}, {'type': 'json', 'value': {}},
                        {'type': 'mapping', 'value': {'bad': 'untyped'}}, {'type': 'private', 'value': 'secret'}):
            with self.subTest(invalid=invalid), self.assertRaises(StorageError):
                decode_value(invalid)

    def test_real_sqlite_document_calls_preserve_revision_and_order(self):
        bound = self.bound('document', DOCUMENT_V1)
        document = decode_value(bound.call_json(_request('put', 'first', {'kind': 'text'}, expected_revision=0)))
        self.assertEqual(document, Document('first', {'kind': 'text'}, 1))
        self.assertEqual(decode_value(bound.call_json(_request('get_many', ['missing', 'first']))), [None, document])
        with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
            bound.call_json(_request('put', 'first', {'kind': 'changed'}, expected_revision=0))
        self.assertEqual(decode_value(bound.call_json(_request('get', 'first'))), document)

    def test_real_sqlite_kv_process_calls_are_binary_safe_and_bound_to_contract(self):
        bound = self.bound('kv', KV_DURABLE_V1)
        self.assertIsNone(decode_value(bound.call_json(_request('set', 'binary', b'\x00\xff'))))
        self.assertEqual(decode_value(bound.call_json(_request('get', 'binary'))), b'\x00\xff')
        for method in ('put_expiring', '_space', 'for_consumer', 'transaction', 'call_json', 'close'):
            with self.subTest(method=method), self.assertRaisesRegex(StorageError, 'STORAGE_PROCESS_METHOD_REJECTED'):
                bound.call_json(_request(method))

    def test_relational_bridge_only_allows_bounded_sql_json_and_rejects_ddl(self):
        bound = self.bound('relational', RELATIONAL_V1)
        bound.prepare(SchemaPackage('records', (Migration(1, ('CREATE TABLE tasks(id TEXT PRIMARY KEY)',)),)))
        bound.seal()
        read = decode_value(bound.call_json(_request('query_json', {'statement': {'sql': 'SELECT 1 AS value'}})))
        self.assertEqual(read['rows'], [{'value': {'type': 'integer', 'value': '1'}}])
        for method in ('prepare', 'transaction', 'batch', 'seal'):
            with self.subTest(method=method), self.assertRaisesRegex(StorageError, 'STORAGE_PROCESS_METHOD_REJECTED'):
                bound.call_json(_request(method))
        with self.assertRaisesRegex(StorageError, 'DATABASE_RUNTIME_STATEMENT_REQUIRED'):
            bound.call_json(_request('query_json', {'statement': {'sql': 'CREATE TABLE forbidden(id INTEGER)'}}))
        read = decode_value(bound.call_json(_request('query_json', {'statement': {
            'sql': "SELECT count(*) AS total FROM sqlite_master WHERE type='table' AND name='forbidden'"}})))
        self.assertEqual(read['rows'][0]['total'], {'type': 'integer', 'value': '0'})

    def test_request_and_result_limits_reject_oversized_values_without_losing_data(self):
        bound = self.bound('kv', KV_DURABLE_V1)
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROCESS_LIMIT'):
            bound.call_json(_request('get', 'x' * MAX_REQUEST_BYTES))
        bound.set('large', b'x' * (MAX_RESULT_BYTES + 1))
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROCESS_LIMIT'):
            bound.call_json(_request('get', 'large'))
        self.assertEqual(len(bound.get('large')), MAX_RESULT_BYTES + 1)

    def test_malformed_payloads_and_too_deep_values_are_rejected(self):
        bound = self.bound('kv', KV_DURABLE_V1)
        for invalid in ({}, {'method': 'get', 'args': 'raw'}, {'method': 'get', 'args': ['untyped']},
                        {'method': 'get', 'kwargs': {'key': {'type': 'bytes', 'value': '!'}}},
                        {'method': 'get', 'timeout': 5}):
            with self.subTest(invalid=invalid), self.assertRaises(StorageError):
                bound.call_json(invalid)
        deep = None
        for _ in range(34):
            deep = [deep]
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROCESS_LIMIT'):
            encode_value(deep)

    def test_disposed_bindings_cannot_call_the_process_bridge(self):
        bound = self.bound('kv', KV_DURABLE_V1)
        self.registry.close()
        with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_UNAVAILABLE'):
            bound.call_json(_request('get', 'key'))
