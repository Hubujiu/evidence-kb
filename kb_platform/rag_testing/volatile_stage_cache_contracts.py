from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.persistence_local import local_database
from kb_platform.plugin_host import PluginHost
from kb_platform.storage_cache_plugins import stage_cache_plugins
from kb_platform.storage_catalog import default_storage_providers, initialize_configuration
from kb_platform.storage_plugins import compile_storage_plugins
from kb_platform.storage_sdk import (
    EphemeralCacheSpace, KV_EXPIRING_V1, KV_V1, StorageError, StorageExport, StorageIdentity, StorageProvider,
)
from .budget import BudgetLedger
from .cache import StageCache, StageKey
from .contracts import digest


def stage_key():
    return StageKey(stage='retrieval', implementation='volatile-cache-contract/v1',
                    upstream=('question-hash', 'ordered-history-hash'), config_hash='config',
                    corpus_hash='corpus', index_snapshot='index', principal='alice',
                    acl_revision='acl-1', model_revision='model', sample_id='sample-1')


class ExpiringMemoryCache:
    def __init__(self):
        self.now, self.values, self.reads = 0, {}, []
        self.available = True
        self.close_count = 0

    def get(self, key):
        self.reads.append(key)
        if not self.available:
            raise StorageError('STORAGE_BACKEND_UNAVAILABLE')
        value = self.values.get(key)
        if value is None or value[1] <= self.now:
            self.values.pop(key, None)
            return None
        return value[0]

    def set(self, key, value):
        self.put_expiring(key, value, ttl_seconds=300)

    def put_expiring(self, key, value, *, ttl_seconds):
        if not self.available:
            raise StorageError('STORAGE_BACKEND_UNAVAILABLE')
        self.values[key] = (value, self.now + ttl_seconds)

    def delete(self, key):
        return self.values.pop(key, None) is not None

    def close(self):
        self.close_count += 1


class VolatileStageCacheContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database, owned = local_database(self.root / 'index.sqlite3', owner='stage-cache')
        self.addCleanup(owned.close)
        self.kv = ExpiringMemoryCache()
        self.cache = StageCache(database=self.database, payload_cache=self.kv, payload_ttl_seconds=10)
        self.key, self.payload = stage_key(), {'evidence': '不可变合成原文', 'offsets': [4, 15]}
        self.cache.put(self.key, self.payload)

    def test_all_exact_dependencies_and_order_are_required_before_kv_lookup(self):
        self.assertEqual(self.cache.get(self.key, authorize=lambda: True), self.payload)
        reads = list(self.kv.reads)
        changes = [{'upstream': ('ordered-history-hash', 'question-hash')},
                   {'upstream': ('different-question', 'ordered-history-hash')},
                   {'upstream': ('question-hash', 'different-history')}]
        changes.extend({field: 'changed'} for field in type(self.key).model_fields if field != 'upstream')
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.cache.get(self.key.model_copy(update=change), authorize=lambda: True))
        self.assertEqual(self.kv.reads, reads)

    def test_current_authorization_and_execution_mode_precede_every_payload_read(self):
        self.assertEqual(self.cache.get(self.key, authorize=lambda: True), self.payload)
        reads = list(self.kv.reads)
        for mode, under_test in (('cold', False), ('cold', True), ('live', True)):
            self.assertIsNone(self.cache.get(self.key, authorize=lambda: True, mode=mode, under_test=under_test))
            with self.assertRaises(PermissionError):
                self.cache.get(self.key, authorize=lambda: False, mode=mode, under_test=under_test)
        with self.assertRaises(PermissionError):
            self.cache.get(self.key, authorize=lambda: False)
        with self.assertRaisesRegex(ValueError, 'unknown cache mode'):
            self.cache.get(self.key, authorize=lambda: True, mode='invalid')
        self.assertEqual(self.kv.reads, reads)
        self.assertEqual(self.cache.get(self.key, authorize=lambda: True, mode='live'), self.payload)

    def test_expiry_outage_and_clear_miss_then_same_output_rebuilds(self):
        self.kv.now = 11
        self.assertIsNone(self.cache.get(self.key, authorize=lambda: True))
        self.kv.available = False
        self.cache.put(self.key, self.payload)
        self.assertIsNone(self.cache.get(self.key, authorize=lambda: True))
        self.kv.available = True
        reopened_database, owned = local_database(self.root / 'index.sqlite3', owner='stage-cache')
        self.addCleanup(owned.close)
        reopened = StageCache(database=reopened_database, payload_cache=self.kv, payload_ttl_seconds=10)
        reopened.put(self.key, self.payload)
        self.assertEqual(reopened.get(self.key, authorize=lambda: True), self.payload)
        self.kv.values.clear()
        self.assertIsNone(reopened.get(self.key, authorize=lambda: True))
        with self.assertRaisesRegex(ValueError, 'stage output changed'):
            reopened.put(self.key, {'evidence': 'different'})
        self.assertEqual(self.kv.values, {})
        reopened.put(self.key, self.payload)
        self.assertEqual(reopened.get(self.key, authorize=lambda: True), self.payload)
        reopened.close()
        self.assertEqual(self.kv.close_count, 0)
        self.assertIsNone(reopened.objects)
        self.assertIsNone(reopened.root)

    def test_malformed_and_forged_payloads_fail_full_integrity_validation(self):
        for value in (b'{', b'\xff', b'{"evidence":"forged"}', 'wrong-type', b'NaN'):
            with self.subTest(value=value):
                self.kv.values[digest(self.payload)] = (value, 10)
                with self.assertRaisesRegex(ValueError, 'cache object integrity failure'):
                    self.cache.get(self.key, authorize=lambda: True)
        self.cache.put(self.key, self.payload)
        self.assertEqual(self.cache.get(self.key, authorize=lambda: True), self.payload)

    def test_volatile_miss_does_not_release_unknown_budget_or_allow_blind_retry(self):
        ledger = BudgetLedger(self.root / 'budget.sqlite3', {'calls': 2, 'tokens': 100})
        self.addCleanup(ledger.close)
        ledger.reserve('physical-unknown', {'calls': 1, 'tokens': 30})
        ledger.finish('physical-unknown', status='UNKNOWN')
        before = ledger.summary()
        self.kv.values.clear()
        self.assertIsNone(self.cache.get(self.key, authorize=lambda: True))
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PHYSICAL_REQUEST'):
            ledger.reserve('must-not-retry', {'calls': 1, 'tokens': 1}, require_settled=True)
        self.assertEqual(ledger.summary(), before)
        self.assertEqual(before['effective_usage_or_reserved'], {'calls': 1, 'tokens': 30})

    def test_persistent_payloads_remain_independent_and_backend_selection_is_explicit(self):
        root = self.root / 'persistent'
        persistent = StageCache(root)
        self.addCleanup(persistent.close)
        persistent.put(self.key, self.payload)
        self.kv.values.clear()
        self.assertEqual(persistent.get(self.key, authorize=lambda: True), self.payload)
        (root / 'objects' / (digest(self.payload) + '.json')).write_text('{"forged":true}', 'utf-8')
        with self.assertRaises(ValueError):
            persistent.get(self.key, authorize=lambda: True)
        for kwargs in ({'root': root, 'payload_cache': self.kv},
                       {'database': self.database, 'objects': object(), 'payload_cache': self.kv}):
            with self.assertRaisesRegex(ValueError, 'CACHE_PAYLOAD_BACKEND_CONFLICT'):
                StageCache(**kwargs)
        with self.assertRaisesRegex(ValueError, 'CACHE_STORAGE_REQUIRED'):
            StageCache(payload_cache=self.kv)
        for ttl in (False, 0, -1, float('nan'), float('inf'), 365 * 24 * 60 * 60 + 1):
            with self.subTest(ttl=ttl), self.assertRaisesRegex(ValueError, 'INVALID_STAGE_CACHE_PAYLOAD_TTL'):
                StageCache(database=self.database, payload_cache=self.kv, payload_ttl_seconds=ttl)

    def test_rust_host_binds_the_reusable_plugin_and_rejects_missing_expiry_contract(self):
        root, kv = self.root / 'host-sqlite', self.kv

        class MemoryProvider:
            def exports(self):
                return (StorageExport('kv', (KV_EXPIRING_V1,), self.open_space),)

            def open_space(self, space, target):
                if not isinstance(target, EphemeralCacheSpace):
                    raise StorageError('STORAGE_CACHE_TARGET_REQUIRED')
                kv.identity = StorageIdentity(space, target.cache_id)
                return kv

            def close(self):
                kv.close()

        providers = default_storage_providers()
        providers['memory'] = StorageProvider(lambda config: MemoryProvider(), {'kv': (KV_EXPIRING_V1,)})
        config = {'version': 2, 'application_id': 'independent-cache-project',
            'instances': {'index': {'provider': 'sqlite', 'config': {'root': str(root)}},
                          'cache': {'provider': 'memory', 'config': {}}},
            'services': {'index': {'instance': 'index', 'export': 'structured'},
                         'payload': {'instance': 'cache', 'export': 'kv'}},
            'bindings': {'cache': {
                'index': {'service': 'index', 'owner': 'stage-cache', 'space': 'results'},
                'payload': {'service': 'payload', 'owner': 'stage-cache', 'space': 'payload', 'cache_id': 'cache-1'}}}}
        config = initialize_configuration(config, provider_catalog=providers)
        profile = {'version': 1, 'plugins': [{'id': 'cache', 'use': 'storage.cache.volatile',
                                             'config': {'payload_ttl_seconds': 10}}]}
        compiled, catalog = compile_storage_plugins(profile, stage_cache_plugins(), config, provider_catalog=providers)
        with PluginHost(compiled, catalog) as host:
            cache = host.service('stage.cache')
            cache.put(self.key, self.payload)
            self.assertEqual(cache.get(self.key, authorize=lambda: True), self.payload)
            cache.close()
            self.assertEqual(kv.close_count, 0)
        self.assertEqual(kv.close_count, 1)
        with self.assertRaises(Exception):
            cache.get(self.key, authorize=lambda: True)
        compiled, catalog = compile_storage_plugins(profile, stage_cache_plugins(), config, provider_catalog=providers)
        with PluginHost(compiled, catalog) as restarted:
            self.assertEqual(restarted.service('stage.cache').get(self.key, authorize=lambda: True), self.payload)
        incompatible = dict(providers)
        incompatible['memory'] = StorageProvider(lambda config: self.fail('must reject before factory'),
                                                  {'kv': (KV_V1,)})
        with self.assertRaisesRegex(StorageError, 'STORAGE_CONTRACT_MISSING'):
            compile_storage_plugins(profile, stage_cache_plugins(), config, provider_catalog=incompatible)
        missing = deepcopy(config)
        del missing['bindings']['cache']['payload']
        with self.assertRaisesRegex(ValueError, 'STORAGE_SLOT_CONFIGURATION_MISMATCH'):
            compile_storage_plugins(profile, stage_cache_plugins(), missing, provider_catalog=providers)
