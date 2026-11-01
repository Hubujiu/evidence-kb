from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.plugin_host import PluginError, PluginHost, PluginSpec
from kb_platform.storage_catalog import default_storage_providers, initialize_configuration
from kb_platform.storage_plugins import compile_storage_plugins, require_storage
from kb_platform.storage_process import decode_value, encode_value
from kb_platform.storage_sdk import (
    EphemeralCacheSpace, ExistingDataset, OBJECT_V1, SpaceKey, StorageError, StorageProvider, StorageSlot,
)
from kb_platform.storage_service import StorageService


def _request(method, *args, **kwargs):
    return {'method': method, 'args': [encode_value(value) for value in args],
            'kwargs': {key: encode_value(value) for key, value in kwargs.items()}}


def _configuration(root):
    return {'version': 2, 'application_id': 'independent',
            'instances': {'files': {'provider': 'files', 'config': {'root': str(root)}}},
            'services': {'files.objects': {'instance': 'files', 'export': 'object'}},
            'bindings': {'tasks': {'objects': {
                'service': 'files.objects', 'owner': 'tasks', 'space': 'originals'}}}}


class StorageObjectContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.providers = default_storage_providers()
        self.space = SpaceKey('independent', 'tasks', 'originals')

    def provider(self, root=None):
        provider = self.providers['files'].factory({'root': str(self.root if root is None else root)})
        self.addCleanup(provider.close)
        return provider

    def bound(self, provider, dataset_id, space=None):
        registry = StorageService()
        self.addCleanup(registry.close)
        registry.register('objects', provider.exports()[0])
        grant = registry.grant('objects', OBJECT_V1, self.space if space is None else space, 'consumer',
                               target=ExistingDataset(dataset_id))
        return registry.bind(grant)

    def test_explicit_initialization_is_idempotent_and_restart_preserves_objects_and_identity(self):
        original = _configuration(self.root)
        initialized = initialize_configuration(original, provider_catalog=self.providers)
        self.assertNotIn('dataset_id', original['bindings']['tasks']['objects'])
        dataset = initialized['bindings']['tasks']['objects']['dataset_id']
        self.assertTrue(dataset)
        self.assertEqual(initialize_configuration(initialized, provider_catalog=self.providers), initialized)
        first = self.provider()
        bound = self.bound(first, dataset)
        ref = bound.put('tenant', b'persisted original')
        self.assertEqual(bound.put('tenant', b'persisted original'), ref)
        first.close()
        restarted = self.bound(self.provider(), dataset)
        self.assertEqual(restarted.identity.dataset_id, dataset)
        self.assertEqual(restarted.read('tenant', ref.locator, sha256=ref.sha256), b'persisted original')
        self.assertEqual(restarted.stat('tenant', ref.locator), ref)

    def test_wrong_dataset_or_empty_directory_cannot_be_opened_as_initialized_storage(self):
        initialized = initialize_configuration(_configuration(self.root), provider_catalog=self.providers)
        dataset = initialized['bindings']['tasks']['objects']['dataset_id']
        with self.assertRaisesRegex(StorageError, 'STORAGE_DATASET_IDENTITY_MISMATCH'):
            self.bound(self.provider(), 'different-dataset')
        empty = self.root / 'mistaken-empty-directory'
        with self.assertRaisesRegex(StorageError, 'STORAGE_DATASET_IDENTITY_MISMATCH'):
            self.bound(self.provider(empty), dataset)
        self.assertEqual(list(empty.iterdir()), [])
        wrong = deepcopy(initialized)
        wrong['bindings']['tasks']['objects']['dataset_id'] = 'different-dataset'
        with self.assertRaisesRegex(StorageError, 'STORAGE_DATASET_IDENTITY_MISMATCH'):
            initialize_configuration(wrong, provider_catalog=self.providers)
        self.assertEqual(initialize_configuration(initialized, provider_catalog=self.providers), initialized)

    def test_object_export_rejects_ephemeral_cache_targets(self):
        provider = self.provider()
        with self.assertRaisesRegex(StorageError, 'STORAGE_PERSISTENT_DATASET_REQUIRED'):
            provider.exports()[0].open_space(self.space, EphemeralCacheSpace('cache'))

    def test_corrupt_identity_or_identity_for_other_space_cannot_open_persistent_objects(self):
        provider = self.provider()
        identity = provider.initialize('object', self.space)
        path = self.root / '.storage-identities' / (self.space.digest + '.json')
        valid = path.read_bytes()
        cases = (b'{broken', json.dumps({'version': 1, 'space': 'other-project/tasks/originals',
                                        'dataset_id': identity.dataset_id}).encode(),
                 json.dumps({'version': 1, 'space': self.space.key, 'dataset_id': ''}).encode())
        for invalid in cases:
            with self.subTest(invalid=invalid):
                path.write_bytes(invalid)
                with self.assertRaisesRegex(StorageError, 'STORAGE_OBJECT_IDENTITY_CORRUPT'):
                    self.bound(provider, identity.dataset_id)
                with self.assertRaisesRegex(StorageError, 'STORAGE_OBJECT_IDENTITY_CORRUPT'):
                    provider.initialize('object', self.space)
                self.assertEqual(path.read_bytes(), invalid)
        path.write_bytes(valid)
        self.assertEqual(self.bound(provider, identity.dataset_id).identity, identity)

    def test_dataset_identity_is_part_of_namespace_and_old_references_do_not_cross_reinitialization(self):
        provider = self.provider()
        first_identity = provider.initialize('object', self.space)
        first = self.bound(provider, first_identity.dataset_id).put('tenant', b'private')
        identity_path = self.root / '.storage-identities' / (self.space.digest + '.json')
        identity_path.unlink()
        second_identity = provider.initialize('object', self.space)
        self.assertNotEqual(first_identity.dataset_id, second_identity.dataset_id)
        second = self.bound(provider, second_identity.dataset_id)
        with self.assertRaisesRegex(ValueError, 'BLOB_ACCESS_DENIED'):
            second.read('tenant', first.locator)
        with self.assertRaisesRegex(StorageError, 'STORAGE_DATASET_IDENTITY_MISMATCH'):
            self.bound(provider, first_identity.dataset_id)
        new_ref = second.put('tenant', b'private')
        self.assertNotEqual(new_ref.locator, first.locator)
        self.assertEqual(second.read('tenant', new_ref.locator), b'private')

    def test_tampered_object_is_rejected_through_new_service_and_is_not_repaired_by_put(self):
        provider = self.provider()
        identity = provider.initialize('object', self.space)
        bound = self.bound(provider, identity.dataset_id)
        ref = bound.put('tenant', b'original')
        path = self.root.joinpath(*ref.object_key.split('/'))
        path.write_bytes(b'corrupt')
        for operation in (lambda: bound.read('tenant', ref.locator),
                          lambda: bound.stat('tenant', ref.locator), lambda: bound.put('tenant', b'original')):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
                operation()
        self.assertEqual(path.read_bytes(), b'corrupt')

    def test_process_bridge_roundtrips_binary_objects_and_rejects_foreign_tenant_or_namespace(self):
        provider = self.provider()
        identity = provider.initialize('object', self.space)
        first = self.bound(provider, identity.dataset_id)
        other_space = SpaceKey('independent', 'other-plugin', 'originals')
        other_identity = provider.initialize('object', other_space)
        other = self.bound(provider, other_identity.dataset_id, other_space)
        ref = decode_value(first.call_json(_request('put', 'tenant', b'\x00\xff\x01')))
        self.assertEqual(decode_value(first.call_json(_request('read', 'tenant', ref.locator))), b'\x00\xff\x01')
        self.assertEqual(decode_value(first.call_json(_request('stat', 'tenant', ref.locator))), ref)
        self.assertEqual(decode_value(first.call_json(_request('reference', 'tenant', ref.locator))), ref)
        for target, tenant in ((first, 'intruder'), (other, 'tenant')):
            for method in ('read', 'stat', 'reference'):
                with self.subTest(method=method, tenant=tenant), self.assertRaises(StorageError):
                    target.call_json(_request(method, tenant, ref.locator))
        for method in ('initialize', 'open_space', 'close', 'for_consumer', '_provider', 'get', 'delete'):
            with self.subTest(method=method), self.assertRaisesRegex(StorageError, 'STORAGE_PROCESS_METHOD_REJECTED'):
                first.call_json(_request(method))
        self.assertEqual(first.read('tenant', ref.locator), b'\x00\xff\x01')

    def test_host_owns_shared_provider_once_and_closed_consumer_cannot_keep_using_objects(self):
        configuration = _configuration(self.root)
        configuration['bindings']['archive'] = {'objects': {
            'service': 'files.objects', 'owner': 'archive', 'space': 'originals'}}
        configuration = initialize_configuration(configuration, provider_catalog=self.providers)
        events = []

        def factory(config):
            provider = self.providers['files'].factory(config)
            close = provider.close

            def tracked_close():
                events.append('closed')
                close()

            provider.close = tracked_close
            return provider

        def mount(ctx, config):
            ctx.provide(ctx.plugin_id, require_storage(ctx, 'objects'))

        names = ('tasks', 'archive')
        specs = tuple(PluginSpec(name, (name,), (), mount,
                                storage_slots=(StorageSlot('objects', OBJECT_V1),)) for name in names)
        providers = {'files': StorageProvider(factory, {'object': (OBJECT_V1,)})}
        profile, catalog = compile_storage_plugins(
            {'version': 1, 'plugins': [{'id': name, 'use': name} for name in names]}, specs, configuration,
            provider_catalog=providers)
        host = PluginHost(profile, catalog)
        self.addCleanup(host.close)
        bound = host.service('tasks')
        ref = bound.put('tenant', b'original')
        self.assertEqual(host.service('archive').put('tenant', b'archive').size, 7)
        self.assertEqual(events, [])
        host.close()
        host.close()
        self.assertEqual(events, ['closed'])
        with self.assertRaises(PluginError):
            bound.read('tenant', ref.locator)


if __name__ == '__main__':
    unittest.main()
