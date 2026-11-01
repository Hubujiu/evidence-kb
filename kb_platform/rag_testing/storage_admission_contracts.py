from types import SimpleNamespace
import unittest

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.storage_plugins import compile_storage_plugins
from kb_platform.storage_sdk import (
    Contract, EphemeralCacheSpace, KeyValueStore, KV_V1, SpaceKey, StorageError, StorageExport,
    StorageIdentity, StorageProvider, StorageSlot,
)
from kb_platform.storage_service import StorageService


class SyntheticKeyValue:
    def __init__(self, identity):
        self.identity = identity
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        return self.values.pop(key, None) is not None


class StorageAdmissionContracts(unittest.TestCase):
    def setUp(self):
        self.space = SpaceKey('synthetic', 'admission', 'cache')
        self.identity = StorageIdentity(self.space, 'fixture-cache')

    def bind(self, implementation):
        registry = StorageService()
        self.addCleanup(registry.close)
        registry.register('fixture', StorageExport('kv', (KV_V1,), lambda space, target: implementation))
        grant = registry.grant('fixture', KV_V1, self.space, 'consumer',
                               target=EphemeralCacheSpace('fixture-cache'))
        return registry.bind(grant)

    def test_malformed_method_cannot_activate_bound_storage(self):
        class MissingArgument(SyntheticKeyValue):
            def set(self, key):
                self.fail = key

        class AsyncMethod(SyntheticKeyValue):
            async def get(self, key):
                return key

        class InheritedStub(KeyValueStore):
            def __init__(self, identity):
                self.identity = identity

        for factory in (MissingArgument, AsyncMethod, InheritedStub):
            with self.subTest(factory=factory), self.assertRaisesRegex(StorageError, 'STORAGE_IMPLEMENTATION_INVALID'):
                self.bind(factory(self.identity))

    def test_independent_implementation_is_accessible_only_through_contract(self):
        implementation = SyntheticKeyValue(self.identity)
        bound = self.bind(implementation)
        bound.set(key='session', value=b'opaque-session')
        self.assertEqual(bound.get(key='session'), b'opaque-session')
        self.assertTrue(bound.delete('session'))
        self.assertIsNone(bound.get('session'))
        with self.assertRaises(AttributeError):
            getattr(bound, 'values')

    def test_provider_declarations_reject_invalid_factory_and_exports(self):
        async def asynchronous_factory(config):
            return config

        for factory in (lambda: None, asynchronous_factory):
            with self.subTest(factory=factory), self.assertRaisesRegex(ValueError, 'INVALID_STORAGE_PROVIDER'):
                StorageProvider(factory, {'kv': (KV_V1,)})
        with self.assertRaisesRegex(ValueError, 'INVALID_STORAGE_PROVIDER'):
            StorageProvider(lambda config: None, {'kv': (KV_V1, KV_V1)})
        with self.assertRaisesRegex(ValueError, 'INVALID_STORAGE_EXPORT'):
            StorageExport('kv', (KV_V1,), lambda space: None)
        with self.assertRaisesRegex(ValueError, 'INVALID_STORAGE_CONTRACT_INTERFACE'):
            Contract('storage.fixture', 1, interface=object())

    def test_invalid_export_is_closed_before_consumer_mounts(self):
        for exports in (None, lambda required: (), lambda: (object(),), lambda: ()):
            with self.subTest(exports=exports):
                events = []
                provider = SimpleNamespace(exports=exports, close=lambda: events.append('closed'))
                catalog = {'fixture': StorageProvider(lambda config: provider, {'kv': (KV_V1,)})}
                spec = PluginSpec('consumer', (), (), lambda ctx, config: events.append('mounted'),
                                  storage_slots=(StorageSlot('cache', KV_V1),))
                profile = {'version': 1, 'plugins': [{'id': 'consumer', 'use': 'consumer'}]}
                config = {'version': 2, 'application_id': 'synthetic',
                    'instances': {'fixture': {'provider': 'fixture', 'config': {}}},
                    'services': {'cache': {'instance': 'fixture', 'export': 'kv'}},
                    'bindings': {'consumer': {'cache': {'service': 'cache', 'owner': 'admission',
                                                       'space': 'cache', 'cache_id': 'fixture-cache'}}}}
                compiled, plugins = compile_storage_plugins(profile, (spec,), config, provider_catalog=catalog)
                with self.assertRaisesRegex(StorageError, 'STORAGE_PROVIDER_'):
                    PluginHost(compiled, plugins)
                self.assertEqual(events, ['closed'])
