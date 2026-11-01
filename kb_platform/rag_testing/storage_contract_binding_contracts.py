from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.database_contracts import Migration, SchemaPackage
from kb_platform.database_providers import SQLiteProvider
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_plugins import compile_storage_plugins, require_storage
from kb_platform.storage_sdk import (
    Contract, EphemeralCacheSpace, ExistingDataset, KV_EXPIRING_V1, KV_V1, RELATIONAL_POSTGRES_V1,
    RELATIONAL_V1, SpaceKey, StorageError, StorageExport, StorageIdentity, StorageProvider, StorageSlot,
)
from kb_platform.storage_service import StorageService
from kb_platform.storage_sdk.relational import GUARDED_RELATIONAL_V1


PRIMARY = StorageSlot('primary', RELATIONAL_V1)


def _configuration(root, dataset_id):
    return {'version': 2, 'application_id': 'independent',
            'instances': {'main': {'provider': 'sqlite', 'config': {'root': str(root)}}},
            'services': {'main.sql': {'instance': 'main', 'export': 'relational'}},
            'bindings': {'tasks': {'primary': {'service': 'main.sql', 'owner': 'tasks',
                                             'space': 'history', 'dataset_id': dataset_id}}}}


def _mount_tasks(ctx, config):
    database = require_storage(ctx, PRIMARY)
    database.prepare(SchemaPackage('tasks', (Migration(1, (
        'CREATE TABLE tasks(id TEXT PRIMARY KEY,value TEXT NOT NULL)',)),)))
    database.seal()
    ctx.provide('tasks', database)


def _provider_catalog(factory=None):
    return {'sqlite': StorageProvider(factory or (lambda config: SqlStorageProvider(SQLiteProvider(config['root']))),
                                     {'relational': (RELATIONAL_V1,), 'structured': (GUARDED_RELATIONAL_V1,)})}


class StorageContractBindingContracts(unittest.TestCase):
    def test_contract_matching_uses_versions_and_explicit_inheritance(self):
        self.assertTrue(RELATIONAL_POSTGRES_V1.satisfies(RELATIONAL_V1))
        self.assertTrue(KV_EXPIRING_V1.satisfies(KV_V1))
        self.assertFalse(KV_V1.satisfies(KV_EXPIRING_V1))
        self.assertFalse(RELATIONAL_V1.satisfies(Contract('storage.relational', 2)))
        self.assertFalse(RELATIONAL_V1.satisfies(Contract('storage.relational', 1)))

    def test_slots_require_typed_contracts_and_unique_names(self):
        for slots in (('primary',), (PRIMARY, PRIMARY), (PRIMARY, StorageSlot('primary', KV_V1))):
            with self.subTest(slots=slots), self.assertRaisesRegex(ValueError, 'INVALID_PLUGIN_STORAGE_SLOTS'):
                PluginSpec('tasks', (), (), lambda ctx, config: None, storage_slots=slots)

    def test_real_sqlite_initialization_is_explicit_and_existing_identity_is_required(self):
        with TemporaryDirectory() as root:
            provider = SqlStorageProvider(SQLiteProvider(root))
            self.addCleanup(provider.close)
            space = SpaceKey('independent', 'tasks', 'history')
            identity = provider.initialize('relational', space)
            export = provider.exports()[0]
            opened = export.open_space(space, ExistingDataset(identity.dataset_id))
            self.assertEqual(opened.identity, identity)
            with self.assertRaisesRegex(StorageError, 'PERSISTENT_DATASET_REQUIRED'):
                export.open_space(space, EphemeralCacheSpace('cache'))
            with self.assertRaisesRegex(ValueError, 'DATASET_ID_REQUIRED'):
                ExistingDataset('')
            with self.assertRaisesRegex(RuntimeError, 'IDENTITY_MISMATCH'):
                export.open_space(space, ExistingDataset('wrong-dataset'))
            with self.assertRaisesRegex(RuntimeError, 'EXPECTED_DATASET_MISSING'):
                export.open_space(SpaceKey('independent', 'tasks', 'empty'), ExistingDataset(identity.dataset_id))

    def test_real_sqlite_rust_host_transactions_restart_and_stale_handle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initializer = SqlStorageProvider(SQLiteProvider(root))
            identity = initializer.initialize('relational', SpaceKey('independent', 'tasks', 'history'))
            initializer.close()
            config = _configuration(root, identity.dataset_id)
            spec = PluginSpec('tasks', ('tasks',), (), _mount_tasks, storage_slots=(PRIMARY,))
            profile = {'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]}
            compiled, catalog = compile_storage_plugins(profile, (spec,), config, provider_catalog=_provider_catalog())
            with PluginHost(compiled, catalog) as host:
                bound = host.service('tasks')
                with bound.transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('persist','first')")
                with self.assertRaisesRegex(RuntimeError, 'rollback'):
                    with bound.transaction() as tx:
                        tx.execute("INSERT INTO tasks VALUES('discard','second')")
                        raise RuntimeError('rollback')
            with self.assertRaises(Exception):
                with bound.transaction():
                    self.fail('stale handle entered transaction')
            compiled, catalog = compile_storage_plugins(profile, (spec,), config, provider_catalog=_provider_catalog())
            with PluginHost(compiled, catalog) as restarted:
                recovered = restarted.service('tasks')
                self.assertEqual(recovered.identity.dataset_id, identity.dataset_id)
                with recovered.transaction(write=False) as tx:
                    self.assertEqual(tx.execute('SELECT id,value FROM tasks').rows, ({'id': 'persist', 'value': 'first'},))

    def test_context_manager_retains_provider_lease_until_exit(self):
        with TemporaryDirectory() as directory:
            provider = SqlStorageProvider(SQLiteProvider(directory))
            self.addCleanup(provider.close)
            key = SpaceKey('app', 'owner', 'data')
            identity = provider.initialize('relational', key)
            registry = StorageService()
            dispose = registry.register('sql', provider.exports()[0])
            grant = registry.grant('sql', RELATIONAL_V1, key, 'consumer', target=ExistingDataset(identity.dataset_id))
            bound = registry.bind(grant)
            self.assertEqual(registry.inspect()['sql']['active_calls'], 0)
            context = bound.transaction(write=False)
            self.assertEqual(registry.inspect()['sql']['active_calls'], 0)
            with context as tx:
                self.assertEqual(tx.execute('SELECT 1').scalar(), 1)
                self.assertEqual(registry.inspect()['sql']['active_calls'], 1)
                with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_BUSY'):
                    dispose()
            self.assertEqual(registry.inspect()['sql']['active_calls'], 0)
            dispose()
            with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_UNAVAILABLE'):
                with bound.transaction():
                    self.fail('disposed handle accepted')

    def test_compiler_rejects_missing_contract_before_factory_or_business_write(self):
        called = []
        spec = PluginSpec('tasks', (), (), lambda ctx, config: None,
                          storage_slots=(StorageSlot('primary', KV_EXPIRING_V1),))
        config = _configuration(Path('unused'), 'dataset')
        with self.assertRaisesRegex(StorageError, 'STORAGE_CONTRACT_MISSING'):
            compile_storage_plugins({'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]},
                (spec,), config, provider_catalog=_provider_catalog(lambda config: called.append(config)))
        self.assertEqual(called, [])

    def test_compiler_requires_v2_identity_all_slots_and_one_service_per_space(self):
        spec = PluginSpec('tasks', (), (), lambda ctx, config: None, storage_slots=(PRIMARY,))
        profile = {'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]}
        base = _configuration(Path('unused'), 'dataset')
        variants = []
        legacy = deepcopy(base)
        legacy['version'] = 1
        variants.append(legacy)
        missing_id = deepcopy(base)
        del missing_id['bindings']['tasks']['primary']['dataset_id']
        variants.append(missing_id)
        missing_slot = deepcopy(base)
        missing_slot['bindings'] = {}
        variants.append(missing_slot)
        extra_slot = deepcopy(base)
        extra_slot['bindings']['tasks']['other'] = deepcopy(base['bindings']['tasks']['primary'])
        variants.append(extra_slot)
        for configuration in variants:
            with self.subTest(configuration=configuration), self.assertRaises(ValueError):
                compile_storage_plugins(profile, (spec,), configuration, provider_catalog=_provider_catalog())

    def test_multiple_exports_close_shared_provider_once(self):
        with TemporaryDirectory() as directory:
            events = []

            class Provider(SqlStorageProvider):
                def exports(self):
                    first = super().exports()[0]
                    return (first, StorageExport('other', first.contracts, first.open_space))

                def close(self):
                    events.append('closed')
                    super().close()

            initial = SqlStorageProvider(SQLiteProvider(directory))
            identity = initial.initialize('relational', SpaceKey('independent', 'tasks', 'history'))
            initial.close()
            config = _configuration(Path(directory), identity.dataset_id)
            config['services']['main.other'] = {'instance': 'main', 'export': 'other'}
            config['bindings']['tasks']['secondary'] = {
                **config['bindings']['tasks']['primary'], 'service': 'main.other'}
            providers = {'sqlite': StorageProvider(lambda values: Provider(SQLiteProvider(values['root'])),
                        {'relational': (RELATIONAL_V1,), 'other': (RELATIONAL_V1,)})}

            def mount(ctx, config):
                _mount_tasks(ctx, config)
                secondary = require_storage(ctx, 'secondary')
                self.assertEqual(secondary.identity.dataset_id, identity.dataset_id)
                self.assertIs(secondary.seal(), secondary)
                ctx.provide('other', secondary)

            spec = PluginSpec('tasks', ('tasks', 'other'), (), mount,
                              storage_slots=(PRIMARY, StorageSlot('secondary', RELATIONAL_V1)))
            profile, specs = compile_storage_plugins({'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]},
                (spec,), config, provider_catalog=providers)
            with PluginHost(profile, specs) as host:
                self.assertEqual(set(host.service('storage.registry').catalog()), {'main.sql', 'main.other'})
                with host.service('tasks').transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('same-space','shared dataset')")
                with host.service('other').transaction(write=False) as tx:
                    self.assertEqual(tx.execute('SELECT value FROM tasks').scalar(), 'shared dataset')
            self.assertEqual(events, ['closed'])

    def test_one_space_cannot_be_silently_redirected_to_another_instance_or_dataset(self):
        spec = PluginSpec('tasks', (), (), lambda ctx, config: None,
                          storage_slots=(PRIMARY, StorageSlot('secondary', RELATIONAL_V1)))
        profile = {'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]}
        base = _configuration(Path('unused'), 'dataset')
        base['instances']['other'] = deepcopy(base['instances']['main'])
        base['services']['other.sql'] = {'instance': 'other', 'export': 'relational'}
        for changed in ({'service': 'other.sql'}, {'dataset_id': 'different'}):
            configuration = deepcopy(base)
            configuration['bindings']['tasks']['secondary'] = {
                **configuration['bindings']['tasks']['primary'], **changed}
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'STORAGE_SPACE_SERVICE_CONFLICT'):
                compile_storage_plugins(profile, (spec,), configuration, provider_catalog=_provider_catalog())

    def test_forged_grants_and_wrong_consumer_are_rejected(self):
        registry = StorageService()
        key = SpaceKey('app', 'owner', 'data')

        class RejectionFixture:
            identity = StorageIdentity(key, 'cache')

            def get(self, key):
                return None

            def set(self, key, value):
                return None

            def delete(self, key):
                return False

        registry.register('cache', StorageExport('kv', (KV_V1,), lambda key, target: RejectionFixture()))
        grant = registry.grant('cache', KV_V1, key, 'authorized', target=EphemeralCacheSpace('cache'))
        with self.assertRaisesRegex(StorageError, 'STORAGE_GRANT_REJECTED'):
            registry.bind(replace(grant, consumer='forged'))
        bound = registry.bind(grant)
        with self.assertRaisesRegex(StorageError, 'STORAGE_CONSUMER_GRANT_MISMATCH'):
            bound.for_consumer({}, lambda: None, consumer_name='other')
        with self.assertRaises(AttributeError):
            bound.put_expiring('key', b'value', ttl_seconds=1)
        with self.assertRaises(AttributeError):
            getattr(bound, '_space')

    def test_registration_change_does_not_redirect_existing_handle(self):
        registry = StorageService()
        key = SpaceKey('app', 'owner', 'data')
        contract = Contract('fixture.lifecycle', 1)
        fixture = type('Fixture', (), {'identity': StorageIdentity(key, 'cache')})()
        export = StorageExport('fixture', (contract,), lambda key, target: fixture)
        first = registry.register('route', export, registration_id='first')
        grant = registry.grant('route', contract, key, 'consumer', target=EphemeralCacheSpace('cache'))
        bound = registry.bind(grant)
        first()
        registry.register('route', export, registration_id='second')
        with self.assertRaisesRegex(StorageError, 'STORAGE_BINDING_CHANGED'), bound._lease():
            self.fail('old registration redirected')
        with self.assertRaisesRegex(StorageError, 'STORAGE_REGISTRATION_REUSED'):
            registry.register('other', export, registration_id='first')

    def test_backend_errors_do_not_expose_credentials(self):
        registry = StorageService()
        contract = Contract('fixture.reject', 1)

        def reject(space, target):
            raise RuntimeError('postgresql://username:secret@example.invalid')

        registry.register('private', StorageExport('fixture', (contract,), reject))
        grant = registry.grant('private', contract, SpaceKey('app', 'owner', 'data'), 'consumer',
                               target=ExistingDataset('existing'))
        with self.assertRaises(StorageError) as caught:
            registry.bind(grant)
        self.assertEqual(caught.exception.code, 'STORAGE_BACKEND_UNAVAILABLE')
        self.assertNotIn('secret', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_third_party_services_are_not_required_to_use_storage_sdk(self):
        def provider(ctx, config):
            ctx.provide('private.service', {'value': 7})

        def consumer(ctx, config):
            ctx.provide('result', ctx.require('private.service')['value'])

        specs = (PluginSpec('private', ('private.service',), (), provider),
                 PluginSpec('consumer', ('result',), ('private.service',), consumer))
        with PluginHost({'version': 1, 'plugins': [{'id': 'p', 'use': 'private'},
                                                {'id': 'c', 'use': 'consumer'}]}, specs) as host:
            self.assertEqual(host.service('result'), 7)

    def test_configuration_fingerprints_change_without_exposing_connection_secrets(self):
        spec = PluginSpec('tasks', (), (), lambda ctx, config: None, storage_slots=(PRIMARY,))
        profile = {'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]}
        configuration = _configuration(Path('unused'), 'dataset')
        first, catalog = compile_storage_plugins(profile, (spec,), configuration, provider_catalog=_provider_catalog())
        initial = {item.key: item.config_inputs({}) for item in catalog if item.config_inputs is not None}
        configuration['instances']['main']['config']['root'] = 'private-connection-secret'
        configuration['bindings']['tasks']['primary']['dataset_id'] = 'another-dataset'
        changed, catalog = compile_storage_plugins(profile, (spec,), configuration, provider_catalog=_provider_catalog())
        later = {item.key: item.config_inputs({}) for item in catalog if item.config_inputs is not None}
        self.assertEqual(first, changed)
        self.assertTrue(all(initial[key] != later[key] for key in initial))
        self.assertNotIn('private-connection-secret', str(later))
        self.assertNotIn('another-dataset', str(later))

    def test_rust_drain_waits_for_complete_transaction_lease(self):
        with TemporaryDirectory() as directory:
            initial = SqlStorageProvider(SQLiteProvider(directory))
            identity = initial.initialize('relational', SpaceKey('independent', 'tasks', 'history'))
            initial.close()
            config = _configuration(Path(directory), identity.dataset_id)
            spec = PluginSpec('tasks', ('tasks',), (), _mount_tasks, storage_slots=(PRIMARY,))
            profile, catalog = compile_storage_plugins({'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]},
                (spec,), config, provider_catalog=_provider_catalog())
            host = PluginHost(profile, catalog)
            self.addCleanup(host.close)
            bound = host.service('tasks')
            registry = host.service('storage.registry')
            with bound.transaction() as tx:
                with self.assertRaises(PluginError) as caught:
                    host.close()
                self.assertEqual(caught.exception.code, 'scope_busy')
                self.assertEqual(registry.inspect()['main.sql']['active_calls'], 1)
                tx.execute("INSERT INTO tasks VALUES('drained','completed before dispose')")
            host.close()
            self.assertEqual(host.inspect()['state'], 'disposed')
            self.assertEqual(registry.catalog(), {})
            with self.assertRaises(PluginError), bound.transaction():
                self.fail('drained transaction accepted')
