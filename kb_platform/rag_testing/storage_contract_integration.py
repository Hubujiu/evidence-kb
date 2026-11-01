from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import inspect

from kb_platform.storage_adapters.redis_cache import RedisCacheProvider
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_adapters.values import (
    DocumentSnapshot, SqlDocumentStore, SqlKeyValueStore, export_documents, import_documents,
)
from kb_platform.storage_sdk import (
    Contract, DOCUMENT_V1, EphemeralCacheSpace, ExistingDataset, KV_DURABLE_V1, KV_EXPIRING_V1,
    KV_V1, RELATIONAL_V1, SpaceKey, StorageError,
)
from kb_platform.storage_service import StorageService
from .cache import StageCache, StageKey
from .contracts import digest


def _sql_provider(bound):
    adapters = {'kv': ((KV_DURABLE_V1,), SqlKeyValueStore)}
    if bound.info.engine in {'sqlite', 'postgresql'}:
        adapters['document'] = ((DOCUMENT_V1,), SqlDocumentStore)
    return SqlStorageProvider(bound._space.provider, adapters=adapters)


def _register(registry, name, provider, export_name):
    export = next(export for export in provider.exports() if export.name == export_name)
    registry.register(name, export)
    return export


def _bind(registry, name, contract, identity, *, consumer='synthetic-consumer'):
    grant = registry.grant(name, contract, identity.space, consumer,
                           target=ExistingDataset(identity.dataset_id))
    return registry.bind(grant)


def _record(fixtures, scenario, **details):
    fixtures.observations.setdefault('storage_contracts', []).append({'scenario': scenario, **details})


def suite(fixtures):
    class StorageContractIntegration(unittest.TestCase):
        def registry(self):
            registry = StorageService()
            self.addCleanup(registry.close)
            return registry

        def sql(self, engine, name, export_name):
            service, database = fixtures.bind(engine, name, owner='storage-' + export_name)
            provider = _sql_provider(database)
            registry = self.registry()
            public_name = engine + '.' + export_name
            export = _register(registry, public_name, provider, export_name)
            contract = DOCUMENT_V1 if export_name == 'document' else KV_DURABLE_V1
            bound = _bind(registry, public_name, contract, database.identity)
            return service, database, registry, provider, export, bound

        def redis(self):
            provider = RedisCacheProvider(fixtures.environment['KB_STORAGE_TEST_REDIS_URL'])
            self.addCleanup(provider.close)
            tracked = []

            def cleanup():
                removed = 0
                errors = []
                remaining = 0
                for cache, keys in tracked:
                    for key in keys:
                        try:
                            removed += int(cache.delete(key))
                        except StorageError as error:
                            errors.append(error.code)
                for cache in {cache._prefix: cache for cache, _ in tracked}.values():
                    try:
                        with provider._connection() as client:
                            remaining += sum(1 for _ in client.scan_iter(match=cache._prefix + '*'))
                    except StorageError as error:
                        errors.append(error.code)
                if remaining:
                    errors.append('STORAGE_ACCEPTANCE_CACHE_KEYS_REMAIN')
                fixtures.observations.setdefault('storage_contract_cache_cleanup', []).append({
                    'backend': 'redis', 'tracked_keys': sum(len(keys) for _, keys in tracked),
                    'removed_keys': removed, 'remaining_keys': remaining, 'errors': errors})
                if errors:
                    raise StorageError('STORAGE_ACCEPTANCE_CACHE_CLEANUP_FAILED')

            self.addCleanup(cleanup)
            return provider, tracked

        def test_document_contract_cas_and_restart_on_postgres_and_sqlite(self):
            for engine in ('sqlite', 'postgresql'):
                with self.subTest(engine=engine):
                    name = 'typed-documents-' + engine
                    service, _, registry, _, _, store = self.sql(engine, name, 'document')
                    first = store.put('policy', {'title': '合成政策', 'values': [1, True, None]}, expected_revision=0)
                    self.assertEqual(first.revision, 1)
                    self.assertEqual(store.get_many(('absent', 'policy', 'policy')), (None, first, first))

                    def compete(index):
                        try:
                            return store.put('policy', {'winner': index}, expected_revision=1).revision
                        except StorageError as error:
                            return error.code

                    with ThreadPoolExecutor(max_workers=4) as pool:
                        outcomes = tuple(pool.map(compete, range(4)))
                    self.assertEqual(outcomes.count(2), 1)
                    self.assertEqual(outcomes.count('STORAGE_REVISION_CONFLICT'), 3)
                    saved = store.get('policy')
                    with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                        store.delete('policy', expected_revision=1)
                    registry.close()
                    service.close()
                    _, _, _, _, _, recovered = self.sql(engine, name, 'document')
                    self.assertEqual(recovered.identity, store.identity)
                    self.assertEqual(recovered.get('policy'), saved)
                    self.assertTrue(recovered.delete('policy', expected_revision=saved.revision))
                    self.assertIsNone(recovered.get('policy'))
                    recreated = recovered.put('policy', {'generation': 'recreated'}, expected_revision=0)
                    self.assertGreater(recreated.revision, saved.revision)
                    with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                        recovered.put('policy', {'generation': 'stale'}, expected_revision=saved.revision)
                    with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                        recovered.delete('policy', expected_revision=saved.revision)
                    self.assertEqual(recovered.get('policy'), recreated)
                    with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_UNAVAILABLE'):
                        store.get('policy')
                    _record(fixtures, 'document-cas-client-restart', backend=engine,
                            identity=asdict(recovered.identity), concurrent_winners=1,
                            concurrent_conflicts=3, revision=recovered.get('policy').revision,
                            stale_cas_after_delete_recreate_rejected=True)

        def test_postgres_sqlite_document_snapshots_migrate_both_directions(self):
            for source_engine, target_engine in (('postgresql', 'sqlite'), ('sqlite', 'postgresql')):
                with self.subTest(source=source_engine, target=target_engine):
                    suffix = source_engine + '-to-' + target_engine
                    _, source_database = fixtures.bind(source_engine, 'typed-snapshot-source-' + suffix,
                                                       owner='storage-document')
                    _, target_database = fixtures.bind(target_engine, 'typed-snapshot-target-' + suffix,
                                                       owner='storage-document')
                    source = SqlDocumentStore(source_database)
                    target = SqlDocumentStore(target_database)
                    source.put('policy', {'text': '不可变合成原文'}, expected_revision=0)
                    source.put('policy', {'text': '经过显式修订的合成原文'}, expected_revision=1)
                    source.put('evidence', {'source': 'synthetic', 'offsets': [4, 12]}, expected_revision=0)
                    deleted = source.put('deleted', {'source': 'deleted-synthetic'}, expected_revision=0)
                    self.assertTrue(source.delete('deleted', expected_revision=deleted.revision))
                    snapshot = export_documents(source, expected_dataset_id=source.identity.dataset_id)
                    restored = DocumentSnapshot.from_json(snapshot.to_json())
                    changed = json.loads(json.dumps(restored.payload))
                    changed['documents'][0]['value'] = {'text': 'corrupted'}
                    for candidate, source_id, target_id in (
                        (replace(restored, payload=changed), source.identity.dataset_id, target.identity.dataset_id),
                        (restored, 'wrong-source', target.identity.dataset_id),
                        (restored, source.identity.dataset_id, 'wrong-target'),
                    ):
                        with self.assertRaises(StorageError):
                            import_documents(target, candidate, expected_source_dataset_id=source_id,
                                             expected_target_dataset_id=target_id)
                        self.assertEqual(target.get_many(('policy', 'evidence')), (None, None))
                    for _ in range(2):
                        count = import_documents(target, restored,
                                                 expected_source_dataset_id=source.identity.dataset_id,
                                                 expected_target_dataset_id=target.identity.dataset_id)
                        self.assertEqual(count, 2)
                    self.assertEqual(target.get_many(('policy', 'evidence')), source.get_many(('policy', 'evidence')))
                    self.assertIsNone(target.get('deleted'))
                    recreated = target.put('deleted', {'source': 'recreated'}, expected_revision=0)
                    self.assertGreater(recreated.revision, deleted.revision)
                    with self.assertRaisesRegex(StorageError, 'STORAGE_REVISION_CONFLICT'):
                        target.put('deleted', {'source': 'stale'}, expected_revision=deleted.revision)
                    _record(fixtures, 'document-snapshot-migration', source_backend=source_engine,
                            target_backend=target_engine, source_identity=asdict(source.identity),
                            target_identity=asdict(target.identity), documents=count,
                            checksum=restored.checksum, rejected_before_write=3, idempotent_reimport=True,
                            tombstone_history_migrated=True, stale_cas_after_migration_rejected=True)

        def test_durable_kv_uses_the_same_contract_on_three_real_backends(self):
            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    name = 'typed-durable-kv-' + engine
                    service, _, registry, _, _, first = self.sql(engine, name, 'kv')
                    _, _, _, _, _, isolated = self.sql(engine, name + '-isolated', 'kv')
                    first.set('Key', b'\x00\xffbinary')
                    first.set('key', b'')
                    first.set('trailing ', b'spaced')
                    first.set('trailing', b'unspaced')
                    isolated.set('Key', b'other-project')
                    self.assertEqual(first.get('Key'), b'\x00\xffbinary')
                    self.assertEqual(isolated.get('Key'), b'other-project')
                    self.assertNotEqual(first.identity, isolated.identity)
                    registry.close()
                    service.close()
                    _, _, _, _, _, recovered = self.sql(engine, name, 'kv')
                    self.assertEqual(recovered.identity, first.identity)
                    self.assertEqual(recovered.get('Key'), b'\x00\xffbinary')
                    self.assertEqual(recovered.get('key'), b'')
                    self.assertEqual(recovered.get('trailing '), b'spaced')
                    self.assertEqual(recovered.get('trailing'), b'unspaced')
                    self.assertTrue(recovered.delete('Key'))
                    self.assertIsNone(recovered.get('Key'))
                    _record(fixtures, 'durable-kv-contract-provider-selection', backend=engine,
                            identity=asdict(recovered.identity), isolated_identity=asdict(isolated.identity),
                            binary_preserved=True, client_restart=True)

        def test_one_postgres_instance_exports_three_distinct_contracts(self):
            _, relational_database = fixtures.bind('postgresql', 'multi-contract-sql', owner='storage-relational')
            _, document_database = fixtures.bind('postgresql', 'multi-contract-document', owner='storage-document')
            _, kv_database = fixtures.bind('postgresql', 'multi-contract-kv', owner='storage-kv')
            provider = _sql_provider(relational_database)
            registry = self.registry()
            identities = {'relational': relational_database.identity, 'document': document_database.identity,
                          'kv': kv_database.identity}
            contracts = {'relational': RELATIONAL_V1, 'document': DOCUMENT_V1, 'kv': KV_DURABLE_V1}
            services = {}
            for name in contracts:
                public_name = 'same-postgres.' + name
                _register(registry, public_name, provider, name)
                services[name] = _bind(registry, public_name, contracts[name], identities[name])
            self.assertEqual(set(registry.catalog()), {'same-postgres.relational', 'same-postgres.document',
                                                       'same-postgres.kv'})
            with services['relational'].transaction(write=False) as tx:
                self.assertEqual(tx.execute('SELECT 23 AS answer').scalar(), 23)
            services['document'].put('record', {'service': 'document'}, expected_revision=0)
            services['kv'].set('record', b'kv')
            self.assertEqual(services['document'].get('record').value, {'service': 'document'})
            self.assertEqual(services['kv'].get('record'), b'kv')
            self.assertEqual(len({item.identity.dataset_id for item in services.values()}), 3)
            with self.assertRaises(AttributeError):
                services['document'].transaction()
            with self.assertRaises(AttributeError):
                services['kv'].put('record', {})
            _record(fixtures, 'one-instance-multiple-contracts', backend='postgresql',
                    contracts=registry.catalog(), identities={name: asdict(value) for name, value in identities.items()},
                    shared_provider_instance=True)

        def test_incompatible_sql_contracts_and_wrong_identity_do_not_create_business_tables(self):
            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    _, database = fixtures.bind(engine, 'typed-rejections-' + engine, owner='storage-kv')
                    registry = self.registry()
                    _register(registry, 'durable.kv', _sql_provider(database), 'kv')
                    with database._space.migration_engine.connect() as audit:
                        self.assertFalse(inspect(audit).has_table(
                            'kb_storage_kv', schema=None if engine == 'sqlite' else database.physical_name))
                    for contract in (KV_EXPIRING_V1, Contract('storage.kv', 2)):
                        with self.assertRaisesRegex(StorageError, 'STORAGE_CONTRACT_MISSING'):
                            registry.grant('durable.kv', contract, database.identity.space, 'consumer',
                                           target=ExistingDataset(database.identity.dataset_id))
                    wrong = registry.grant('durable.kv', KV_DURABLE_V1, database.identity.space, 'consumer',
                                           target=ExistingDataset('wrong-dataset'))
                    with self.assertRaisesRegex(StorageError, 'IDENTITY_MISMATCH'):
                        registry.bind(wrong)
                    with database._space.migration_engine.connect() as audit:
                        self.assertFalse(inspect(audit).has_table(
                            'kb_storage_kv', schema=None if engine == 'sqlite' else database.physical_name))
                    _record(fixtures, 'binding-rejected-before-business-write', backend=engine,
                            identity=asdict(database.identity), missing_ttl=True, wrong_contract_version=True,
                            wrong_dataset=True, business_table_created=False)

        def test_redis_ttl_space_isolation_client_reconnect_and_cache_rebuild(self):
            provider, tracked = self.redis()
            space = SpaceKey('storage-acceptance-' + fixtures.run_id, 'storage-cache', 'redis-runtime')
            target = EphemeralCacheSpace('cache-' + fixtures.run_id)
            cache = provider.open_space(space, target)
            isolated = provider.open_space(SpaceKey(space.application_id, 'other-cache-owner', space.space_id), target)
            keys = ('shared-key', 'expires', 'reconnect', 'rebuild')
            tracked.extend(((cache, keys), (isolated, keys)))
            cache.set('shared-key', b'first')
            isolated.set('shared-key', b'second')
            self.assertEqual(cache.get('shared-key'), b'first')
            self.assertEqual(isolated.get('shared-key'), b'second')
            cache.put_expiring('expires', b'expiring', ttl_seconds=1)
            self.assertEqual(cache.get('expires'), b'expiring')
            deadline = monotonic() + 10
            while cache.get('expires') is not None and monotonic() < deadline:
                sleep(0.02)
            self.assertIsNone(cache.get('expires'))
            cache.set('reconnect', b'client-reconnect')
            provider._client.connection_pool.disconnect()
            self.assertEqual(cache.get('reconnect'), b'client-reconnect')
            replacement, replacement_tracked = self.redis()
            reopened = replacement.open_space(space, target)
            replacement_tracked.append((reopened, keys))
            self.assertEqual(reopened.get('reconnect'), b'client-reconnect')
            cache.set('rebuild', b'cached')
            self.assertTrue(cache.delete('rebuild'))
            self.assertIsNone(reopened.get('rebuild'))
            reopened.put_expiring('rebuild', b'rebuilt', ttl_seconds=30)
            self.assertEqual(cache.get('rebuild'), b'rebuilt')
            _record(fixtures, 'redis-cache-ttl-isolation-reconnect-rebuild', backend='redis',
                    identity=asdict(cache.identity), isolated_identity=asdict(isolated.identity),
                    ttl_expired=True, client_reconnect=True, cache_rebuilt=True, persistent=False)

        def test_real_redis_rejects_persistent_contract_and_identity_without_writing(self):
            provider, tracked = self.redis()
            space = SpaceKey('storage-acceptance-' + fixtures.run_id, 'storage-cache', 'redis-rejections')
            target = EphemeralCacheSpace('cache-' + fixtures.run_id)
            raw_cache = provider.open_space(space, target)
            tracked.append((raw_cache, ('probe',)))
            registry = self.registry()
            _register(registry, 'redis.kv', provider, 'kv')
            with self.assertRaisesRegex(StorageError, 'STORAGE_CONTRACT_MISSING'):
                registry.grant('redis.kv', KV_DURABLE_V1, space, 'consumer', target=target)
            wrong = registry.grant('redis.kv', KV_V1, space, 'consumer',
                                   target=ExistingDataset('not-a-cache-identity'))
            with self.assertRaisesRegex(StorageError, 'STORAGE_CACHE_TARGET_REQUIRED'):
                registry.bind(wrong)
            valid = registry.grant('redis.kv', KV_EXPIRING_V1, space, 'consumer', target=target)
            cache = registry.bind(valid)
            self.assertIsNone(cache.get('probe'))
            with self.assertRaisesRegex(StorageError, 'STORAGE_INVALID_TTL'):
                cache.put_expiring('probe', b'must-not-write', ttl_seconds=0)
            with provider._connection() as client:
                self.assertEqual(tuple(client.scan_iter(match=raw_cache._prefix + '*')), ())
            _record(fixtures, 'redis-persistent-contract-rejected-before-write', backend='redis',
                    identity=asdict(cache.identity), persistent_contract_rejected=True,
                    persistent_identity_rejected=True, invalid_ttl_rejected=True, business_keys=0)

        def test_stage_cache_consumes_real_redis_with_three_actual_relational_indexes(self):
            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    name = 'volatile-stage-cache-' + engine
                    service, database = fixtures.bind(engine, name, owner='stage-cache')
                    provider, tracked = self.redis()
                    space = SpaceKey('storage-acceptance-' + fixtures.run_id, 'stage-cache', name)
                    target = EphemeralCacheSpace('stage-payload-' + fixtures.run_id)
                    raw_cache = provider.open_space(space, target)
                    registry = self.registry()
                    _register(registry, 'redis.stage-payload', provider, 'kv')
                    grant = registry.grant('redis.stage-payload', KV_EXPIRING_V1, space,
                                           'stage-cache', target=target)
                    payload_cache = registry.bind(grant)
                    key = StageKey(stage='retrieval', implementation='storage-acceptance/volatile-v1',
                        upstream=('synthetic-question', 'ordered-history'), config_hash='config',
                        corpus_hash='corpus', index_snapshot='index', principal='alice',
                        acl_revision='acl-1', model_revision='local-fixture', sample_id=engine)
                    payload = {'evidence': '不可变合成证据', 'source': 'synthetic', 'offsets': [4, 12]}
                    content_hash = digest(payload)
                    tracked.append((raw_cache, (content_hash, digest({'evidence': 'changed'}))))
                    cache = StageCache(database=database, payload_cache=payload_cache, payload_ttl_seconds=30)
                    cache.put(key, payload)
                    self.assertEqual(cache.get(key, authorize=lambda: True), payload)
                    with self.assertRaises(PermissionError):
                        cache.get(key, authorize=lambda: False)
                    self.assertIsNone(cache.get(key, authorize=lambda: True, mode='cold'))
                    self.assertIsNone(cache.get(key, authorize=lambda: True, mode='live', under_test=True))
                    self.assertIsNone(cache.get(key.model_copy(update={'principal': 'bob'}), authorize=lambda: True))
                    raw_cache.set(content_hash, b'{"evidence":"forged"}')
                    with self.assertRaisesRegex(ValueError, 'cache object integrity failure'):
                        cache.get(key, authorize=lambda: True)
                    cache.put(key, payload)
                    with patch.object(provider._client, 'get', side_effect=RedisConnectionError('injected outage')):
                        self.assertIsNone(cache.get(key, authorize=lambda: True))
                    with patch.object(provider._client, 'set', side_effect=RedisConnectionError('injected outage')):
                        cache.put(key, payload)
                    provider._client.connection_pool.disconnect()
                    self.assertEqual(cache.get(key, authorize=lambda: True), payload)
                    raw_cache.put_expiring(content_hash, raw_cache.get(content_hash), ttl_seconds=0.05)
                    deadline = monotonic() + 10
                    while raw_cache.get(content_hash) is not None and monotonic() < deadline:
                        sleep(0.02)
                    self.assertIsNone(cache.get(key, authorize=lambda: True))
                    with self.assertRaisesRegex(ValueError, 'stage output changed'):
                        cache.put(key, {'evidence': 'changed'})
                    self.assertIsNone(raw_cache.get(digest({'evidence': 'changed'})))
                    cache.put(key, payload)
                    self.assertTrue(raw_cache.delete(content_hash))
                    self.assertIsNone(cache.get(key, authorize=lambda: True))
                    service.close()
                    with self.assertRaises(RuntimeError):
                        cache.get(key, authorize=lambda: True)
                    _, recovered_database = fixtures.bind(engine, name, owner='stage-cache')
                    recovered = StageCache(database=recovered_database, payload_cache=payload_cache,
                                           payload_ttl_seconds=30)
                    self.assertIsNone(recovered.get(key, authorize=lambda: True))
                    recovered.put(key, payload)
                    recovered.close()
                    self.assertEqual(recovered.get(key, authorize=lambda: True), payload)
                    _record(fixtures, 'volatile-stage-cache-real-redis', index_backend=engine,
                            index_identity=asdict(recovered_database.identity),
                            cache_identity=asdict(raw_cache.identity), ttl_expired=True,
                            injected_transport_outage_returns_miss=True, dirty_payload_rejected=True,
                            same_output_rebuilt=True, changed_output_rejected=True,
                            stale_index_rejected=True, payload_lifecycle_owned_by_provider=True,
                            model_calls=0, embedding_calls=0, judge_calls=0)

    return unittest.defaultTestLoader.loadTestsFromTestCase(StorageContractIntegration)
