from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.search import SearchUnavailable, backend_dependencies
from kb_platform.storage_plugins import compile_storage_plugins, require_storage
from kb_platform.storage_sdk import (
    DOCUMENT_V1, LEXICAL_BM25_V1, SpaceKey, StorageExport, StorageIdentity, StorageProvider, StorageSlot,
    VECTOR_V1, VectorStore,
)
from kb_platform.storage_service import StorageService


def declared(revision='foreign-vector/1'):
    return {'version': 1, 'implementation': {'revision': revision}, 'storage': {'dataset': 'stable-dataset'}}


class SearchDependencyContracts(unittest.TestCase):
    def test_unknown_implementation_declares_its_dependencies_without_global_registration(self):
        class IndependentProvider:
            def dependency_manifest(self):
                return declared()

        implementation = IndependentProvider()
        first = backend_dependencies(implementation)
        self.assertEqual(first, declared())
        first['implementation']['revision'] = 'mutated output'
        self.assertEqual(backend_dependencies(implementation), declared())
        implementation.dependency_manifest = lambda: declared('foreign-vector/2')
        self.assertNotEqual(backend_dependencies(implementation), declared())

    def test_missing_or_invalid_dependency_declaration_never_becomes_reusable(self):
        with self.assertRaisesRegex(SearchUnavailable, 'manifest required'):
            backend_dependencies(object())
        for value in ({}, None, {'version': True, 'implementation': {'revision': 'v1'}, 'storage': {'id': 'x'}},
                      {'version': 1, 'implementation': {}, 'storage': {'id': 'x'}},
                      declared() | {'invalid': float('nan')}):
            with self.subTest(value=value), self.assertRaisesRegex(SearchUnavailable, 'manifest'):
                backend_dependencies(SimpleNamespace(dependency_manifest=lambda: value))

    def test_document_vector_and_lexical_contracts_refuse_missing_dependency_declaration_at_binding(self):
        space = SpaceKey('independent', 'vectors', 'products')
        registry = StorageService()
        self.addCleanup(registry.close)
        from kb_platform.storage_sdk import ExistingDataset, StorageError

        for contract in (DOCUMENT_V1, VECTOR_V1, LEXICAL_BM25_V1):
            methods = {key: Mock() for key in vars(contract.interface or contract.extends[0].interface)
                       if not key.startswith('_') and key != 'dependency_manifest'}
            implementation = SimpleNamespace(identity=StorageIdentity(space, 'stable-dataset'), **methods)
            registry.register(contract.key, StorageExport('records', (contract,), lambda *args: implementation))
            grant = registry.grant(contract.key, contract, space, 'consumer', target=ExistingDataset('stable-dataset'))
            with self.subTest(contract=contract.key), self.assertRaisesRegex(StorageError, 'STORAGE_IMPLEMENTATION_INVALID'):
                registry.bind(grant)

    def test_invalid_declared_manifest_is_rejected_at_binding(self):
        from kb_platform.storage_sdk import ExistingDataset, StorageError

        space = SpaceKey('independent', 'vectors', 'products')
        implementation = Mock(spec=VectorStore)
        implementation.identity = StorageIdentity(space, 'stable-dataset')
        implementation.dependency_manifest.return_value = {}
        registry = StorageService()
        self.addCleanup(registry.close)
        registry.register('vectors', StorageExport('vector', (VECTOR_V1,), lambda *args: implementation))
        grant = registry.grant('vectors', VECTOR_V1, space, 'consumer', target=ExistingDataset('stable-dataset'))
        with self.assertRaisesRegex(StorageError, 'STORAGE_IMPLEMENTATION_INVALID'):
            registry.bind(grant)

    def test_registration_restart_is_stable_while_config_environment_and_implementation_changes_invalidate(self):
        slot = StorageSlot('search', VECTOR_V1)
        revision = ['foreign-vector/1']

        def factory(config):
            def open_space(space, target):
                implementation = Mock(spec=VectorStore)
                implementation.identity = StorageIdentity(space, target.dataset_id)
                implementation.dependency_manifest = lambda: declared(revision[0])
                return implementation

            return SimpleNamespace(exports=lambda: (StorageExport('vector', (VECTOR_V1,), open_space),),
                                   close=lambda: None)

        consumer = PluginSpec('independent.consumer', ('consumer',), (),
            lambda context, config: context.provide('consumer', require_storage(context, slot)), storage_slots=(slot,))
        profile = {'version': 1, 'plugins': [{'id': 'consumer', 'use': consumer.key}]}
        configuration = {'version': 2, 'application_id': 'independent',
            'instances': {'foreign': {'provider': 'unlisted-provider', 'config': {'credential_env': 'DEPENDENCY_SECRET'}}},
            'services': {'vectors': {'instance': 'foreign', 'export': 'vector'}},
            'bindings': {'consumer': {'search': {'service': 'vectors', 'owner': 'vectors',
                'space': 'products', 'dataset_id': 'stable-dataset'}}}}
        providers = {'unlisted-provider': StorageProvider(factory, {'vector': (VECTOR_V1,)})}

        def snapshot(config, secret):
            with patch.dict('os.environ', {'DEPENDENCY_SECRET': secret}):
                selected, catalog = compile_storage_plugins(profile, (consumer,), config, provider_catalog=providers)
                with PluginHost(selected, catalog) as host:
                    bound = host.service('consumer')
                    result = backend_dependencies(bound)
                    registration = (host.runtime_id, bound.registration_id)
            self.assertNotIn('registration_id', json.dumps(result))
            self.assertNotIn(secret, json.dumps(result))
            return result, registration

        first, first_registration = snapshot(configuration, 'synthetic-secret-one')
        restarted, next_registration = snapshot(configuration, 'synthetic-secret-one')
        self.assertNotEqual(first_registration, next_registration)
        self.assertEqual(first, restarted)
        self.assertNotEqual(first, snapshot(configuration, 'synthetic-secret-two')[0])
        changed = deepcopy(configuration)
        changed['instances']['foreign']['config']['metric'] = 'cosine'
        self.assertNotEqual(first, snapshot(changed, 'synthetic-secret-one')[0])
        revision[0] = 'foreign-vector/2'
        self.assertNotEqual(first, snapshot(configuration, 'synthetic-secret-one')[0])
