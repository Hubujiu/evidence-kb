from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.database_contracts import DatabaseError
from kb_platform.storage_catalog import default_storage_providers, initialize_configuration
from kb_platform.storage_configuration import convert_database_configuration, main
from kb_platform.storage_sdk import (
    EphemeralCacheSpace, ExistingDataset, KV_EXPIRING_V1, SpaceKey, StorageError, StorageProvider,
)


def database_configuration():
    return {
        'version': 1,
        'application_id': 'configuration-contracts',
        'routes': {
            'online': {'provider': 'postgres', 'migration_dsn_env': 'TEST_MIGRATION_DSN',
                       'options': {'connect_args': {'sslmode': 'verify-full'}}},
            'local': {'provider': 'sqlite', 'root': 'sqlite-root'},
        },
        'bindings': {
            'sessions': {'database': {'route': 'local', 'owner': 'sessions', 'space': 'main',
                                      'expected_dataset_id': 'frozen-dataset'}},
            'documents': {'database': {'route': 'online', 'owner': 'documents', 'space': 'main'}},
        },
    }


def sqlite_configuration(root, *, export='document'):
    return {
        'version': 2,
        'application_id': 'configuration-contracts',
        'instances': {'local': {'provider': 'sqlite', 'config': {'root': str(root)}}},
        'services': {'records': {'instance': 'local', 'export': export}},
        'bindings': {'consumer': {'records': {'service': 'records', 'owner': 'records', 'space': 'main'}}},
    }


def run_configuration_cli(operation, source, target):
    stdout, stderr = StringIO(), StringIO()
    arguments = ['storage_configuration', operation, '--input', str(source), '--output', str(target)]
    code = 0
    with patch.object(sys, 'argv', arguments), redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            main()
        except SystemExit as error:
            code = error.code
    return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


class StorageConfigurationConversionContracts(unittest.TestCase):
    def test_explicit_conversion_preserves_input_and_only_translates_configuration_fields(self):
        original = database_configuration()
        frozen = deepcopy(original)
        converted = convert_database_configuration(original)
        self.assertEqual(original, frozen)
        self.assertEqual(converted['version'], 2)
        self.assertEqual(converted['application_id'], frozen['application_id'])
        self.assertEqual(converted['instances']['online'], {
            'provider': 'postgresql',
            'config': {'migration_dsn_env': 'TEST_MIGRATION_DSN',
                       'options': {'connect_args': {'sslmode': 'verify-full'}}},
        })
        self.assertEqual(converted['instances']['local'], {
            'provider': 'sqlite', 'config': {'root': 'sqlite-root'},
        })
        self.assertEqual(converted['services']['local.relational'], {
            'instance': 'local', 'export': 'relational',
        })
        self.assertEqual(converted['bindings']['sessions']['database'], {
            'owner': 'sessions', 'space': 'main', 'dataset_id': 'frozen-dataset',
            'service': 'local.relational',
        })
        self.assertNotIn('dataset_id', converted['bindings']['documents']['database'])
        converted['instances']['online']['config']['options']['connect_args']['sslmode'] = 'changed'
        self.assertEqual(original, frozen)

    def test_conversion_rejects_wrong_version_and_unknown_top_level_fields(self):
        for update in ({'version': 2}, {'unreviewed': True}):
            with self.subTest(update=update):
                configuration = database_configuration() | update
                with self.assertRaisesRegex(ValueError, 'INVALID_DATABASE_CONFIGURATION'):
                    convert_database_configuration(configuration)

    def test_cli_conversion_writes_new_output_without_touching_the_input(self):
        with TemporaryDirectory() as temporary:
            source, target = Path(temporary) / 'source.json', Path(temporary) / 'converted.json'
            payload = json.dumps(database_configuration(), indent=3).encode('utf-8') + b'\n'
            source.write_bytes(payload)
            result = run_configuration_cli('convert-databases', source, target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), payload)
            self.assertEqual(json.loads(target.read_text('utf-8')),
                             convert_database_configuration(database_configuration()))
            self.assertEqual(json.loads(result.stdout)['status'], 'COMPLETE')

    def test_cli_refuses_existing_output_and_same_input_output(self):
        with TemporaryDirectory() as temporary:
            source, target = Path(temporary) / 'source.json', Path(temporary) / 'prior.json'
            payload = json.dumps(database_configuration()).encode('utf-8')
            source.write_bytes(payload)
            target.write_bytes(b'prior immutable evidence\n')
            for destination in (target, source):
                with self.subTest(destination=destination.name):
                    result = run_configuration_cli('convert-databases', source, destination)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('Output must be a new file', result.stderr)
                    self.assertEqual(source.read_bytes(), payload)
                    self.assertEqual(target.read_bytes(), b'prior immutable evidence\n')


class StorageConfigurationInitializationContracts(unittest.TestCase):
    def test_sqlite_explicit_initialization_and_reopen_preserve_identity_and_document(self):
        with TemporaryDirectory() as temporary:
            configuration = sqlite_configuration(Path(temporary) / 'sqlite')
            frozen = deepcopy(configuration)
            initialized = initialize_configuration(configuration)
            self.assertEqual(configuration, frozen)
            dataset = initialized['bindings']['consumer']['records']['dataset_id']
            self.assertTrue(dataset)
            space = SpaceKey('configuration-contracts', 'records', 'main')
            provider = default_storage_providers()['sqlite'].factory(configuration['instances']['local']['config'])
            try:
                exports = {export.name: export for export in provider.exports()}
                store = exports['document'].open_space(space, ExistingDataset(dataset))
                saved = store.put('source', {'text': '原文'}, expected_revision=0)
            finally:
                provider.close()
            reopened = initialize_configuration(initialized)
            self.assertEqual(reopened, initialized)
            provider = default_storage_providers()['sqlite'].factory(configuration['instances']['local']['config'])
            try:
                exports = {export.name: export for export in provider.exports()}
                store = exports['document'].open_space(space, ExistingDataset(dataset))
                self.assertEqual(store.identity.dataset_id, dataset)
                self.assertEqual(store.get('source'), saved)
            finally:
                provider.close()

    def test_cli_initialization_keeps_template_unchanged_and_reopens_same_dataset(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target, reopened = root / 'template.json', root / 'configured.json', root / 'reopened.json'
            payload = json.dumps(sqlite_configuration(root / 'sqlite')).encode('utf-8')
            source.write_bytes(payload)
            first = run_configuration_cli('initialize', source, target)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(source.read_bytes(), payload)
            configured = target.read_bytes()
            second = run_configuration_cli('initialize', target, reopened)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(target.read_bytes(), configured)
            self.assertEqual(json.loads(reopened.read_text('utf-8')), json.loads(configured))

    def test_existing_dataset_on_empty_target_is_rejected_without_creating_that_identity(self):
        with TemporaryDirectory() as temporary:
            configuration = sqlite_configuration(Path(temporary) / 'sqlite', export='relational')
            configuration['bindings']['consumer']['records']['dataset_id'] = 'expected-existing-dataset'
            frozen = deepcopy(configuration)
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_EXPECTED_DATASET_MISSING'):
                initialize_configuration(configuration)
            self.assertEqual(configuration, frozen)
            del configuration['bindings']['consumer']['records']['dataset_id']
            initialized = initialize_configuration(configuration)
            self.assertNotEqual(initialized['bindings']['consumer']['records']['dataset_id'],
                                'expected-existing-dataset')

    def test_wrong_dataset_is_rejected_and_does_not_replace_initialized_identity(self):
        with TemporaryDirectory() as temporary:
            initialized = initialize_configuration(sqlite_configuration(Path(temporary) / 'sqlite'))
            changed = deepcopy(initialized)
            changed['bindings']['consumer']['records']['dataset_id'] = 'wrong-existing-dataset'
            with self.assertRaisesRegex(DatabaseError, 'DATABASE_DATASET_IDENTITY_MISMATCH'):
                initialize_configuration(changed)
            self.assertEqual(initialize_configuration(initialized), initialized)

    def test_empty_dataset_and_cache_target_cannot_initialize_persistent_storage(self):
        with TemporaryDirectory() as temporary:
            configuration = sqlite_configuration(Path(temporary) / 'sqlite')
            configuration['bindings']['consumer']['records']['dataset_id'] = ''
            with self.assertRaisesRegex(ValueError, 'STORAGE_DATASET_ID_REQUIRED'):
                initialize_configuration(configuration)
            provider = default_storage_providers()['sqlite'].factory(configuration['instances']['local']['config'])
            try:
                exports = {export.name: export for export in provider.exports()}
                with self.assertRaisesRegex(StorageError, 'STORAGE_PERSISTENT_DATASET_REQUIRED'):
                    exports['document'].open_space(SpaceKey('configuration-contracts', 'records', 'main'),
                                                   EphemeralCacheSpace('cache-only'))
            finally:
                provider.close()

    def test_same_sqlite_space_can_initialize_multiple_exports_with_one_identity(self):
        with TemporaryDirectory() as temporary:
            configuration = sqlite_configuration(Path(temporary) / 'sqlite')
            configuration['services']['sql'] = {'instance': 'local', 'export': 'relational'}
            configuration['bindings']['consumer']['sql'] = {
                'service': 'sql', 'owner': 'records', 'space': 'main',
            }
            initialized = initialize_configuration(configuration)
            bindings = initialized['bindings']['consumer']
            self.assertEqual(bindings['records']['dataset_id'], bindings['sql']['dataset_id'])

    def test_cache_id_is_preserved_without_initializing_a_durable_dataset(self):
        configuration = sqlite_configuration('unused', export='kv')
        configuration['instances']['local']['provider'] = 'cache-fixture'
        configuration['bindings']['consumer']['records']['cache_id'] = 'rebuildable-cache'
        factory = Mock(side_effect=RuntimeError('cache initialization must not contact a server'))
        initialized = initialize_configuration(
            configuration, provider_catalog={'cache-fixture': StorageProvider(factory, {'kv': (KV_EXPIRING_V1,)})})
        self.assertEqual(initialized, configuration)
        self.assertNotIn('dataset_id', initialized['bindings']['consumer']['records'])
        factory.assert_not_called()

    def test_cache_and_dataset_targets_are_mutually_exclusive_and_cache_id_is_required(self):
        for target, error in (
            ({'cache_id': 'cache', 'dataset_id': 'dataset'}, 'INVALID_STORAGE_BINDING'),
            ({'cache_id': ''}, 'STORAGE_CACHE_ID_REQUIRED'),
        ):
            with self.subTest(target=target):
                configuration = sqlite_configuration('unused', export='kv')
                configuration['bindings']['consumer']['records'].update(target)
                with self.assertRaisesRegex(ValueError, error):
                    initialize_configuration(configuration)
