from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from redis.exceptions import ConnectionError as RedisConnectionError

from kb_platform.database_contracts import Migration, SchemaPackage, SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.storage_adapters.redis_cache import RedisCacheProvider
from kb_platform.storage_adapters.values import (
    DocumentSnapshot, MAX_VALUE_BYTES, SqlDocumentStore, SqlKeyValueStore, export_documents, import_documents,
)
from kb_platform.storage_sdk import (
    DOCUMENT_V1, EphemeralCacheSpace, ExistingDataset, KV_DURABLE_V1, KV_EXPIRING_V1, StorageError,
)


@contextmanager
def value_store(root, *, kind=SqlDocumentStore, application='app', owner='documents', space='main', dataset=None):
    provider = SQLiteProvider(root)
    service = DatabaseService()
    service.register('sqlite', provider)
    grant = service.grant('sqlite', SpaceKey(application, owner, space), 'synthetic-consumer',
                          expected_dataset_id=dataset)
    try:
        yield kind(service.bind(grant))
    finally:
        service.close()


class StorageDocumentContracts(unittest.TestCase):
    def test_document_roundtrip_versions_and_ordered_bounded_reads(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            DOCUMENT_V1.validate(store)
            self.assertIsNone(store.get('missing'))
            first = store.put('中文/a', {'nested': [None, True, 1, {'text': '原文'}]}, expected_revision=0)
            self.assertEqual(first.revision, 1)
            changed = store.put('中文/a', {'updated': 2}, expected_revision=1)
            self.assertEqual(changed.revision, 2)
            self.assertEqual(store.get_many(('missing', '中文/a', '中文/a')), (None, changed, changed))
            self.assertEqual(store.get_many(()), ())
            self.assertFalse(store.delete('missing'))
            self.assertTrue(store.delete('中文/a', expected_revision=2))
            self.assertIsNone(store.get('中文/a'))
            with self.assertRaisesRegex(StorageError, 'STORAGE_LIMIT_EXCEEDED'):
                store.get_many(tuple(str(index) for index in range(101)))

    def test_conditional_update_and_delete_never_overwrite_new_revision(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            saved = store.put('record', {'value': 1})
            for operation in (
                lambda: store.put('record', {'value': 9}, expected_revision=0),
                lambda: store.put('record', {'value': 9}, expected_revision=3),
                lambda: store.delete('record', expected_revision=0),
                lambda: store.delete('record', expected_revision=3),
            ):
                with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                    operation()
                self.assertEqual(store.get('record'), saved)

    def test_concurrent_compare_and_swap_has_exactly_one_winner(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            store.put('record', {'winner': None}, expected_revision=0)

            def change(index):
                try:
                    return store.put('record', {'winner': index}, expected_revision=1)
                except StorageError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = tuple(pool.map(change, range(8)))
            self.assertEqual(sum(item == 'STORAGE_REVISION_CONFLICT' for item in results), 7)
            self.assertEqual(store.get('record').revision, 2)

    def test_concurrent_create_has_exactly_one_winner(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            def create(index):
                try:
                    return store.put('record', {'winner': index}, expected_revision=0)
                except StorageError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = tuple(pool.map(create, range(6)))
            self.assertEqual(sum(item == 'STORAGE_REVISION_CONFLICT' for item in results), 5)
            self.assertEqual(store.get('record').revision, 1)

    def test_delete_recreate_does_not_reuse_a_revision_or_accept_old_cas(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            original = store.put('record', {'generation': 'original'}, expected_revision=0)
            self.assertTrue(store.delete('record', expected_revision=original.revision))
            self.assertIsNone(store.get('record'))
            self.assertEqual(store.get_many(('record', 'missing')), (None, None))
            self.assertFalse(store.delete('record', expected_revision=original.revision))
            with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                store.put('record', {'generation': 'stale'}, expected_revision=original.revision)
            recreated = store.put('record', {'generation': 'recreated'}, expected_revision=0)
            self.assertEqual(recreated.revision, 3)
            for operation in (
                lambda: store.put('record', {'generation': 'stale'}, expected_revision=original.revision),
                lambda: store.delete('record', expected_revision=original.revision),
            ):
                with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                    operation()
                self.assertEqual(store.get('record'), recreated)

    def test_concurrent_old_cas_cannot_update_or_delete_a_recreated_document(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            original = store.put('record', {'generation': 'original'})
            store.delete('record')
            recreated = store.put('record', {'generation': 'recreated'}, expected_revision=0)

            def stale(index):
                try:
                    if index % 2:
                        store.delete('record', expected_revision=original.revision)
                    else:
                        store.put('record', {'stale': index}, expected_revision=original.revision)
                    return 'UNEXPECTED_WRITE'
                except StorageError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = tuple(pool.map(stale, range(8)))
            self.assertEqual(results, ('STORAGE_REVISION_CONFLICT',) * 8)
            self.assertEqual(store.get('record'), recreated)

    def test_concurrent_create_after_delete_has_one_winner_and_a_new_revision(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            store.put('record', {'generation': 'original'})
            store.delete('record')

            def create(index):
                try:
                    return store.put('record', {'winner': index}, expected_revision=0).revision
                except StorageError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = tuple(pool.map(create, range(6)))
            self.assertEqual(results.count(3), 1)
            self.assertEqual(results.count('STORAGE_REVISION_CONFLICT'), 5)

    def test_tombstone_survives_restart_and_unconditional_recreation_stays_monotonic(self):
        with TemporaryDirectory() as root:
            with value_store(root) as store:
                original = store.put('record', {'generation': 'original'})
                store.delete('record', expected_revision=original.revision)
                dataset = store.identity.dataset_id
            with value_store(root, dataset=dataset) as recovered:
                self.assertIsNone(recovered.get('record'))
                recreated = recovered.put('record', {'generation': 'after-restart'})
                self.assertEqual(recreated.revision, 3)
            with value_store(root, dataset=dataset) as restarted:
                self.assertEqual(restarted.get('record'), recreated)
                with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                    restarted.put('record', {}, expected_revision=original.revision)
                with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                    restarted.delete('record', expected_revision=original.revision)

    def test_explicit_schema_migration_preserves_existing_live_revisions(self):
        with TemporaryDirectory() as root:
            service = DatabaseService()
            service.register('sqlite', SQLiteProvider(root))
            try:
                database = service.bind(service.grant('sqlite', SpaceKey('app', 'documents', 'main'), 'fixture'))
                database.prepare(SchemaPackage('documents', (Migration(1, (
                    'CREATE TABLE kb_storage_documents(key_hash VARCHAR(64) PRIMARY KEY,item_key TEXT NOT NULL,'
                    'payload TEXT NOT NULL,revision BIGINT NOT NULL '
                    'CHECK(revision>0 AND revision<=9223372036854775807),UNIQUE(item_key))',
                )),)))
                with database.transaction() as tx:
                    tx.execute('INSERT INTO kb_storage_documents(key_hash,item_key,payload,revision) '
                               'VALUES(:key_hash,:item_key,:payload,7)',
                               {'key_hash': sha256(b'record').hexdigest(), 'item_key': 'record', 'payload': '{}'})
                dataset = database.identity.dataset_id
            finally:
                service.close()
            with value_store(root, dataset=dataset) as migrated:
                self.assertEqual(migrated.get('record').revision, 7)
                self.assertTrue(migrated.delete('record', expected_revision=7))
                self.assertEqual(migrated.put('record', {}, expected_revision=0).revision, 9)

    def test_invalid_json_and_revision_inputs_fail_before_write(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            invalid = ([1], {1: 'coerced'}, {'number': float('nan')}, {'number': float('inf')},
                       {'tuple': (1, 2)}, {'bytes': b'x'})
            for value in invalid:
                with self.subTest(value=repr(value)), self.assertRaises(StorageError):
                    store.put('invalid', value)
            for revision in (-1, True, 1.5):
                with self.subTest(revision=revision), self.assertRaisesRegex(StorageError, 'INVALID_REVISION'):
                    store.put('invalid', {}, expected_revision=revision)
            self.assertIsNone(store.get('invalid'))
            with self.assertRaisesRegex(StorageError, 'LIMIT_EXCEEDED'):
                store.put('oversized', {'text': 'x' * MAX_VALUE_BYTES})

    def test_key_injection_cannot_escape_space(self):
        with TemporaryDirectory() as root:
            with value_store(root, application='first') as first, value_store(root, application='second') as second:
                key = "../../other'; DROP TABLE kb_storage_documents; --"
                first.put(key, {'value': 'first'})
                second.put(key, {'value': 'second'})
                self.assertEqual(first.get(key).value, {'value': 'first'})
                self.assertEqual(second.get(key).value, {'value': 'second'})
                for bad in ('', 'a\x00b', '字' * 200, '\ud800'):
                    with self.subTest(key=bad), self.assertRaisesRegex(StorageError, 'INVALID_KEY'):
                        first.get(bad)

    def test_restart_requires_the_same_dataset_and_preserves_values(self):
        with TemporaryDirectory() as root:
            with value_store(root) as store:
                stored = store.put('restart', {'value': 'persisted'})
                dataset = store.identity.dataset_id
            with value_store(root, dataset=dataset) as reopened:
                self.assertEqual(reopened.get('restart'), stored)

    def test_corrupt_json_or_revision_is_not_returned_as_a_valid_document(self):
        with TemporaryDirectory() as root, value_store(root) as store:
            store.put('corrupt', {})
            with store._database.transaction() as tx:
                tx.execute('UPDATE kb_storage_documents SET payload=:payload', {'payload': '[1,2]'})
            with self.assertRaisesRegex(StorageError, 'STORAGE_DATA_CORRUPT'):
                store.get('corrupt')

    def test_snapshot_migration_preserves_versions_and_is_idempotent(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target:
                source.put('a', {'text': 'first'})
                source.put('a', {'text': 'second'})
                source.put('b', {'text': 'other'}, expected_revision=0)
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                decoded = DocumentSnapshot.from_json(snapshot.to_json())
                for _ in range(2):
                    count = import_documents(target, decoded,
                                             expected_source_dataset_id=source.identity.dataset_id,
                                             expected_target_dataset_id=target.identity.dataset_id)
                    self.assertEqual(count, 2)
                    self.assertEqual(target.get_many(('a', 'b')), source.get_many(('a', 'b')))

    def test_snapshot_migrations_preserve_deleted_revision_history(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target, \
                    value_store(Path(root) / 'third') as third:
                original = source.put('record', {'generation': 'original'})
                source.delete('record', expected_revision=original.revision)
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                self.assertEqual(snapshot.payload['format'], 'storage-document-snapshot/v2')
                self.assertEqual(snapshot.payload['documents'], [])
                self.assertEqual(snapshot.payload['tombstones'], [{'id': 'record', 'revision': 2}])
                changed = json.loads(json.dumps(snapshot.payload))
                changed['tombstones'][0]['revision'] = 1
                with self.assertRaisesRegex(StorageError, 'STORAGE_INVALID_SNAPSHOT'):
                    import_documents(target, replace(snapshot, payload=changed),
                                     expected_source_dataset_id=source.identity.dataset_id,
                                     expected_target_dataset_id=target.identity.dataset_id)
                for _ in range(2):
                    self.assertEqual(import_documents(target, DocumentSnapshot.from_json(snapshot.to_json()),
                        expected_source_dataset_id=source.identity.dataset_id,
                        expected_target_dataset_id=target.identity.dataset_id), 0)
                self.assertIsNone(target.get('record'))
                recreated = target.put('record', {'generation': 'recreated'}, expected_revision=0)
                self.assertEqual(recreated.revision, 3)
                with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                    target.delete('record', expected_revision=original.revision)
                target.delete('record', expected_revision=recreated.revision)
                second = export_documents(target, expected_dataset_id=target.identity.dataset_id)
                import_documents(third, second, expected_source_dataset_id=target.identity.dataset_id,
                                 expected_target_dataset_id=third.identity.dataset_id)
                self.assertEqual(third.put('record', {}, expected_revision=0).revision, 5)
                for old_revision in (original.revision, recreated.revision):
                    with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                        third.put('record', {'stale': True}, expected_revision=old_revision)
                    with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                        third.delete('record', expected_revision=old_revision)

    def test_snapshot_requires_version_history_and_rejects_live_tombstone_overlap(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target:
                source.put('record', {})
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                old = json.loads(json.dumps(snapshot.payload))
                old['format'] = 'storage-document-snapshot/v1'
                del old['tombstones']
                overlap = json.loads(json.dumps(snapshot.payload))
                overlap['tombstones'] = [{'id': 'record', 'revision': 2}]
                for payload in (old, overlap):
                    checksum = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                                 separators=(',', ':')).encode()).hexdigest()
                    with self.assertRaisesRegex(StorageError, 'STORAGE_INVALID_SNAPSHOT'):
                        import_documents(target, DocumentSnapshot(payload, checksum),
                                         expected_source_dataset_id=source.identity.dataset_id,
                                         expected_target_dataset_id=target.identity.dataset_id)
                    self.assertIsNone(target.get('record'))

    def test_import_cannot_erase_a_target_deleted_revision_history(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target:
                source.put('shared', {'value': 1})
                target.put('shared', {'value': 1})
                original = target.put('deleted', {})
                target.delete('deleted', expected_revision=original.revision)
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                with self.assertRaisesRegex(StorageError, 'STORAGE_IMPORT_TARGET_NOT_EMPTY'):
                    import_documents(target, snapshot, expected_source_dataset_id=source.identity.dataset_id,
                                     expected_target_dataset_id=target.identity.dataset_id)
                self.assertEqual(target.put('deleted', {}, expected_revision=0).revision, 3)

    def test_snapshot_corruption_and_wrong_identity_never_write(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target:
                source.put('a', {'text': 'unchanged'})
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                payload = json.loads(json.dumps(snapshot.payload))
                payload['documents'][0]['value']['text'] = 'tampered'
                corrupt = replace(snapshot, payload=payload)
                for candidate, source_id, target_id in (
                    (corrupt, source.identity.dataset_id, target.identity.dataset_id),
                    (snapshot, 'wrong', target.identity.dataset_id),
                    (snapshot, source.identity.dataset_id, 'wrong'),
                ):
                    with self.subTest(source_id=source_id, target_id=target_id), self.assertRaises(StorageError):
                        import_documents(target, candidate, expected_source_dataset_id=source_id,
                                         expected_target_dataset_id=target_id)
                    self.assertIsNone(target.get('a'))

    def test_snapshot_rejects_duplicate_ids_and_nonempty_target(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target:
                source.put('a', {'text': 'source'})
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                payload = json.loads(json.dumps(snapshot.payload))
                payload['documents'].append(payload['documents'][0])
                checksum = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                             separators=(',', ':')).encode()).hexdigest()
                with self.assertRaisesRegex(StorageError, 'INVALID_SNAPSHOT'):
                    import_documents(target, DocumentSnapshot(payload, checksum),
                                     expected_source_dataset_id=source.identity.dataset_id,
                                     expected_target_dataset_id=target.identity.dataset_id)
                target.put('a', {'text': 'target'})
                with self.assertRaisesRegex(StorageError, 'IMPORT_TARGET_NOT_EMPTY'):
                    import_documents(target, snapshot, expected_source_dataset_id=source.identity.dataset_id,
                                     expected_target_dataset_id=target.identity.dataset_id)
                self.assertEqual(target.get('a').value, {'text': 'target'})

    def test_interrupted_import_rolls_back_all_documents_and_can_retry(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target:
                source.put('a', {'text': 'first'})
                source.put('b', {'text': 'second'})
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                original = target._database.transaction

                @contextmanager
                def interrupted(**kwargs):
                    with original(**kwargs) as tx:
                        execute = tx.execute
                        writes = 0

                        def fail_second_insert(sql, params=None):
                            nonlocal writes
                            if sql.startswith('INSERT'):
                                writes += 1
                                if writes == 2:
                                    raise RuntimeError('SYNTHETIC_IMPORT_INTERRUPTED')
                            return execute(sql, params)

                        tx.execute = fail_second_insert
                        yield tx

                with patch.object(target._database, 'transaction', interrupted):
                    with self.assertRaisesRegex(RuntimeError, 'SYNTHETIC_IMPORT_INTERRUPTED'):
                        import_documents(target, snapshot, expected_source_dataset_id=source.identity.dataset_id,
                                         expected_target_dataset_id=target.identity.dataset_id)
                self.assertEqual(target.get_many(('a', 'b')), (None, None))
                self.assertEqual(import_documents(target, snapshot,
                                                   expected_source_dataset_id=source.identity.dataset_id,
                                                   expected_target_dataset_id=target.identity.dataset_id), 2)

    def test_import_uses_the_content_validated_by_its_checksum(self):
        with TemporaryDirectory() as root:
            with value_store(Path(root) / 'source') as source, value_store(Path(root) / 'target') as target:
                source.put('a', {'text': 'validated'})
                snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                original = target._transaction

                @contextmanager
                def changed_after_validation(**kwargs):
                    snapshot.payload['documents'][0]['value']['text'] = 'mutated-after-validation'
                    with original(**kwargs) as tx:
                        yield tx

                with patch.object(target, '_transaction', changed_after_validation):
                    import_documents(target, snapshot, expected_source_dataset_id=source.identity.dataset_id,
                                     expected_target_dataset_id=target.identity.dataset_id)
                self.assertEqual(target.get('a').value, {'text': 'validated'})


class StorageKeyValueContracts(unittest.TestCase):
    def test_durable_binary_roundtrip_restart_and_key_case(self):
        with TemporaryDirectory() as root:
            with value_store(root, kind=SqlKeyValueStore) as store:
                KV_DURABLE_V1.validate(store)
                self.assertIsNone(store.get('missing'))
                store.set('Key', b'\x00\xffvalue')
                store.set('key', b'')
                store.set('trailing ', b'space')
                store.set('trailing', b'no-space')
                dataset = store.identity.dataset_id
            with value_store(root, kind=SqlKeyValueStore, dataset=dataset) as store:
                self.assertEqual(store.get('Key'), b'\x00\xffvalue')
                self.assertEqual(store.get('key'), b'')
                self.assertEqual(store.get('trailing '), b'space')
                self.assertEqual(store.get('trailing'), b'no-space')
                store.set('Key', b'updated')
                self.assertEqual(store.get('Key'), b'updated')
                self.assertTrue(store.delete('Key'))
                self.assertFalse(store.delete('Key'))

    def test_binary_values_are_bounded_and_require_bytes(self):
        with TemporaryDirectory() as root, value_store(root, kind=SqlKeyValueStore) as store:
            for invalid in ('text', bytearray(b'data'), b'x' * (MAX_VALUE_BYTES + 1)):
                with self.subTest(value_type=type(invalid)), self.assertRaises(StorageError):
                    store.set('invalid', invalid)
            self.assertIsNone(store.get('invalid'))


class RedisCacheBoundaryContracts(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.patcher = patch('kb_platform.storage_adapters.redis_cache.redis.Redis.from_url', return_value=self.client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.provider = RedisCacheProvider('redis://synthetic-secret@localhost:6379/0')
        self.addCleanup(self.provider.close)
        self.space = SpaceKey('project', 'consumer', 'cache')
        self.cache = self.provider.open_space(self.space, EphemeralCacheSpace('cache-identity'))

    def test_cache_only_exports_expiring_kv(self):
        export = self.provider.exports()[0]
        KV_EXPIRING_V1.validate(self.cache)
        self.assertFalse(any(contract.satisfies(KV_DURABLE_V1) for contract in export.contracts))
        with self.assertRaisesRegex(StorageError, 'CACHE_TARGET_REQUIRED'):
            self.provider.open_space(self.space, ExistingDataset('persistent-id'))
        with self.assertRaisesRegex(StorageError, 'CACHE_DATASET_UNSUPPORTED'):
            self.provider.initialize('kv', self.space)

    def test_fixed_namespaces_keep_user_keys_and_other_projects_separate(self):
        other = self.provider.open_space(SpaceKey('other-project', 'consumer', 'cache'),
                                         EphemeralCacheSpace('cache-identity'))
        rotated = self.provider.open_space(self.space, EphemeralCacheSpace('other-cache'))
        malicious = '}:other-project:*\x01../../data'
        self.cache.set(malicious, b'cache')
        other.set(malicious, b'other')
        rotated.set(malicious, b'rotated')
        keys = [call.args[0] for call in self.client.set.call_args_list]
        self.assertEqual(len(set(keys)), 3)
        self.assertTrue(all(malicious not in key for key in keys))
        self.assertEqual(self.cache.identity.dataset_id, 'cache-identity')

    def test_ttl_is_positive_finite_bounded_and_converted_to_milliseconds(self):
        for ttl in (0, -1, float('inf'), float('nan'), True, 366 * 24 * 60 * 60):
            with self.subTest(ttl=ttl), self.assertRaisesRegex(StorageError, 'INVALID_TTL'):
                self.cache.put_expiring('key', b'data', ttl_seconds=ttl)
        self.client.set.assert_not_called()
        self.cache.put_expiring('key', b'data', ttl_seconds=0.0011)
        self.assertEqual(self.client.set.call_args.kwargs, {'px': 2})

    def test_connection_errors_are_redacted_and_closed_handles_rejected(self):
        self.client.get.side_effect = RedisConnectionError('redis://synthetic-secret@localhost')
        with self.assertRaises(StorageError) as caught:
            self.cache.get('key')
        self.assertEqual(str(caught.exception), 'STORAGE_BACKEND_UNAVAILABLE')
        self.assertNotIn('synthetic-secret', repr(caught.exception))
        self.provider.close()
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROVIDER_CLOSED'):
            self.cache.set('key', b'data')
        self.provider.close()
        self.client.close.assert_called_once()

    def test_url_cannot_override_binary_timeouts_or_retry_options(self):
        for suffix in ('?decode_responses=True', '?socket_timeout=9999', '?retry_on_timeout=True', '#fragment'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(StorageError, 'INVALID_REDIS_CONFIG'):
                RedisCacheProvider('redis://localhost:6379/0' + suffix)

    def test_failed_close_seals_admission_and_retries_physical_cleanup(self):
        self.client.close.side_effect = [RuntimeError('close failed'), None]
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            self.provider.close()
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROVIDER_CLOSED'):
            self.cache.get('key')
        self.provider.close()
        self.provider.close()
        self.assertEqual(self.client.close.call_count, 2)

    def test_reentrant_close_during_operation_waits_for_operation_to_leave(self):
        self.client.get.side_effect = lambda key: self.provider.close()
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROVIDER_BUSY'):
            self.cache.get('key')
        self.client.close.assert_not_called()
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROVIDER_CLOSED'):
            self.cache.get('key')
        self.provider.close()
        self.client.close.assert_called_once()

    def test_reentrant_close_during_cleanup_is_rejected_and_retryable(self):
        self.client.close.side_effect = self.provider.close
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROVIDER_BUSY'):
            self.provider.close()
        self.client.close.side_effect = None
        self.provider.close()
        self.assertEqual(self.client.close.call_count, 2)
