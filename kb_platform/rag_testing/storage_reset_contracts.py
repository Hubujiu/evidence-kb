from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from kb_platform.storage_catalog import default_storage_providers, initialize_configuration
from kb_platform.storage_reset import initialize_business_reset, main, plan_business_reset, reset_to_file
from kb_platform.storage_sdk import ExistingDataset, KV_EXPIRING_V1, SpaceKey, StorageProvider


def configuration(root):
    bindings = {name: {'data': {'service': 'business', 'owner': name, 'space': 'current'}}
                for name in ('repository', 'search', 'originals', 'store', 'context', 'cache')}
    bindings['provider'] = {'ledger': {'service': 'financial', 'owner': 'provider', 'space': 'ledger'}}
    return {'version': 2, 'application_id': 'reset-contract',
        'instances': {
            'business': {'provider': 'sqlite', 'config': {'root': str(root / 'business')}},
            'financial': {'provider': 'finance', 'config': {'root': str(root / 'financial')}}},
        'services': {'business': {'instance': 'business', 'export': 'document'},
                     'financial': {'instance': 'financial', 'export': 'document'}},
        'bindings': bindings}


def providers():
    sqlite = default_storage_providers()['sqlite']
    return {'sqlite': sqlite, 'finance': sqlite}


def document(configuration, consumer, catalog, *, value=None):
    binding = next(iter(configuration['bindings'][consumer].values()))
    service = configuration['services'][binding['service']]
    instance = configuration['instances'][service['instance']]
    provider = catalog[instance['provider']].factory(instance['config'])
    try:
        exports = {export.name: export for export in provider.exports()}
        space = SpaceKey(binding.get('application_id', configuration['application_id']),
                         binding['owner'], binding['space'])
        storage = exports[service['export']].open_space(space, ExistingDataset(binding['dataset_id']))
        if value is not None:
            storage.put('sentinel', value, expected_revision=0)
        return storage.get('sentinel')
    finally:
        provider.close()


class StorageResetContracts(unittest.TestCase):
    def test_business_spaces_are_empty_and_old_data_and_financial_state_are_untouched(self):
        with TemporaryDirectory() as temporary:
            root, catalog = Path(temporary), providers()
            original = initialize_configuration(configuration(root), provider_catalog=catalog)
            for consumer in original['bindings']:
                document(original, consumer, catalog, value={'status': 'UNKNOWN', 'reserved': 23})
            frozen = deepcopy(original)
            financial_before = {path.name: path.read_bytes() for path in (root / 'financial').rglob('*')
                                if path.is_file()}
            protected = StorageProvider(Mock(side_effect=AssertionError('financial provider opened')),
                                        catalog['finance'].exports)
            reset_catalog = {**catalog, 'finance': protected}
            plan = plan_business_reset(original, provider_catalog=reset_catalog)
            result = initialize_business_reset(plan, provider_catalog=reset_catalog)
            self.assertEqual(original, frozen)
            self.assertEqual(result['bindings']['provider'], original['bindings']['provider'])
            self.assertEqual(financial_before, {path.name: path.read_bytes()
                for path in (root / 'financial').rglob('*') if path.is_file()})
            for consumer in plan.consumers:
                self.assertIsNone(document(result, consumer, catalog))
                self.assertEqual(document(original, consumer, catalog).value,
                                 {'status': 'UNKNOWN', 'reserved': 23})
                old = next(iter(original['bindings'][consumer].values()))
                new = next(iter(result['bindings'][consumer].values()))
                self.assertNotEqual(old['dataset_id'], new['dataset_id'])
                self.assertNotEqual(old['space'], new['space'])

    def test_shared_business_space_keeps_a_single_new_identity(self):
        with TemporaryDirectory() as temporary:
            catalog = providers()
            value = configuration(Path(temporary))
            value['bindings']['search']['data'] = deepcopy(value['bindings']['repository']['data'])
            initialized = initialize_configuration(value, provider_catalog=catalog)
            result = initialize_business_reset(plan_business_reset(initialized, provider_catalog=catalog),
                                               provider_catalog=catalog)
            self.assertEqual(result['bindings']['repository']['data'], result['bindings']['search']['data'])

    def test_cache_gets_new_namespace_without_contacting_cache_provider(self):
        catalog = {'cache': StorageProvider(Mock(side_effect=AssertionError('cache provider opened')),
                                           {'kv': (KV_EXPIRING_V1,)})}
        value = {'version': 2, 'application_id': 'cache-reset',
            'instances': {'cache': {'provider': 'cache', 'config': {}}},
            'services': {'cache': {'instance': 'cache', 'export': 'kv'}},
            'bindings': {'cache': {'payload': {'service': 'cache', 'owner': 'cache',
                'space': 'payload', 'cache_id': 'previous'}}}}
        plan = plan_business_reset(value, provider_catalog=catalog)
        result = initialize_business_reset(plan, provider_catalog=catalog)
        binding = result['bindings']['cache']['payload']
        self.assertNotEqual(binding['cache_id'], 'previous')
        self.assertNotEqual(binding['space'], 'payload')
        self.assertNotIn('dataset_id', binding)

    def test_roles_are_exhaustive_and_ledgers_cannot_be_reset(self):
        with TemporaryDirectory() as temporary:
            catalog = providers()
            value = initialize_configuration(configuration(Path(temporary)), provider_catalog=catalog)
            for roles in ({}, {'provider': 'knowledge'},
                          {name: 'knowledge' for name in value['bindings']},
                          {name: 'ledger' for name in value['bindings']}):
                with self.subTest(roles=roles), self.assertRaises(ValueError):
                    plan_business_reset(value, roles=roles, provider_catalog=catalog)
            value['bindings']['custom'] = deepcopy(value['bindings']['context'])
            with self.assertRaisesRegex(ValueError, 'RESET_EXPLICIT_CONSUMER_ROLES_REQUIRED'):
                plan_business_reset(value, provider_catalog=catalog)
            roles = {name: 'knowledge' for name in value['bindings']}
            roles['provider'] = 'ledger'
            roles['custom'] = 'context'
            self.assertIn('custom', plan_business_reset(value, roles=roles, provider_catalog=catalog).consumers)

    def test_invalid_late_binding_is_rejected_before_any_provider_is_created(self):
        with TemporaryDirectory() as temporary:
            catalog = providers()
            value = initialize_configuration(configuration(Path(temporary)), provider_catalog=catalog)
            value['bindings']['store']['data']['service'] = 'missing'
            forbidden = {name: StorageProvider(Mock(side_effect=AssertionError('provider opened')), provider.exports)
                         for name, provider in catalog.items()}
            with self.assertRaisesRegex(ValueError, 'RESET_INITIALIZED_BINDING_REQUIRED'):
                plan_business_reset(value, provider_catalog=forbidden)
            for provider in forbidden.values():
                provider.factory.assert_not_called()

    def test_atomic_publication_never_overwrites_input_or_existing_output(self):
        with TemporaryDirectory() as temporary:
            root, catalog = Path(temporary), providers()
            value = initialize_configuration(configuration(root), provider_catalog=catalog)
            source, output = root / 'source.json', root / 'new.json'
            raw = json.dumps(value, indent=3).encode('utf-8')
            source.write_bytes(raw)
            result = reset_to_file(source, output, provider_catalog=catalog)
            self.assertEqual(result['status'], 'COMPLETE')
            self.assertEqual(result['preserved_consumers'], ['provider'])
            self.assertEqual(source.read_bytes(), raw)
            published = output.read_bytes()
            for destination in (source, output):
                with self.assertRaisesRegex(ValueError, 'RESET_OUTPUT_MUST_BE_NEW'):
                    reset_to_file(source, destination, provider_catalog=catalog)
            self.assertEqual(output.read_bytes(), published)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(list(root.glob('.storage-reset-*')), [])

    def test_failed_initialization_leaves_no_published_configuration(self):
        with TemporaryDirectory() as temporary:
            root, catalog = Path(temporary), providers()
            value = initialize_configuration(configuration(root), provider_catalog=catalog)
            source, output = root / 'source.json', root / 'new.json'
            raw = json.dumps(value).encode('utf-8')
            source.write_bytes(raw)
            failed = {**catalog, 'sqlite': StorageProvider(Mock(side_effect=RuntimeError('unavailable')),
                                                         catalog['sqlite'].exports)}
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                reset_to_file(source, output, provider_catalog=failed)
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(list(root.glob('.storage-reset-*')), [])

    def test_competing_output_is_not_replaced(self):
        with TemporaryDirectory() as temporary:
            root, catalog = Path(temporary), providers()
            value = initialize_configuration(configuration(root), provider_catalog=catalog)
            source, output = root / 'source.json', root / 'new.json'
            source.write_text(json.dumps(value), 'utf-8')

            def initialize(plan, **kwargs):
                output.write_bytes(b'other writer')
                return initialize_business_reset(plan, **kwargs)

            with patch('kb_platform.storage_reset.initialize_business_reset', side_effect=initialize):
                with self.assertRaises(FileExistsError):
                    reset_to_file(source, output, provider_catalog=catalog)
            self.assertEqual(output.read_bytes(), b'other writer')
            self.assertEqual(list(root.glob('.storage-reset-*')), [])

    def test_cli_uses_explicit_operation_and_does_not_print_configuration(self):
        with TemporaryDirectory() as temporary:
            root, catalog = Path(temporary), providers()
            value = initialize_configuration(configuration(root), provider_catalog=catalog)
            source, output, roles = root / 'source.json', root / 'new.json', root / 'roles.json'
            source.write_text(json.dumps(value), 'utf-8')
            roles.write_text(json.dumps({name: 'ledger' if name == 'provider' else 'knowledge'
                                        for name in value['bindings']}), 'utf-8')
            arguments = ['storage_reset', '--input', str(source), '--output', str(output), '--roles', str(roles)]
            stdout = StringIO()
            with patch.object(sys, 'argv', arguments), redirect_stdout(stdout), \
                    patch('kb_platform.storage_reset.default_storage_providers', return_value=catalog), \
                    patch('kb_platform.storage_catalog.default_storage_providers', return_value=catalog):
                self.assertEqual(main(), 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report['preserved_consumers'], ['provider'])
            self.assertNotIn('instances', report)
            self.assertTrue(output.is_file())
