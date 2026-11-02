import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kb_platform.application_composition import create_root_application
from kb_platform.core import Platform
from kb_platform.database_contracts import Migration, SchemaPackage, SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.harness_profile import create_agent_application
from kb_platform.plugin_host import PluginSpec
from kb_platform.session_identity import Actor
from kb_platform.settings import Settings
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_plugins import require_storage
from kb_platform.storage_sdk import KV_V1, RELATIONAL_V1, StorageError, StorageProvider, StorageSlot
from kb_platform.storage_sdk.relational import GUARDED_RELATIONAL_V1


class StorageCompositionContracts(unittest.TestCase):
    def configuration(self, root, consumer, slot='primary', owner='records', export='relational'):
        initializer = SqlStorageProvider(SQLiteProvider(root / 'databases'))
        try:
            identity = initializer.initialize(export, SpaceKey('standalone-project', owner, 'history'))
        finally:
            initializer.close()
        return {'version': 2, 'application_id': 'standalone-project',
            'instances': {'main': {'provider': 'sqlite', 'config': {'root': str(root / 'databases')}}},
            'services': {'main.storage': {'instance': 'main', 'export': export}},
            'bindings': {consumer: {slot: {'service': 'main.storage', 'owner': owner, 'space': 'history',
                                          'dataset_id': identity.dataset_id}}}}

    def test_standalone_agent_uses_configured_database_and_recovers_after_host_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'store', export='structured')
            path = root / 'databases.json'
            path.write_text(json.dumps(configuration), encoding='utf-8')
            environment = {'KB_STORAGE_CONFIG': str(path)}
            actor = Actor('tenant', 'owner')
            profile = {'version': 1, 'bundles': ['core'], 'plugins': [
                {'id': 'store', 'use': 'harness.store.database', 'config': {}}]}
            application, host = create_agent_application(profile=profile, environment=environment, model=lambda request: None)
            try:
                session = application.create(actor, 'independent-task')
                dataset = host.service('session.store').identity.dataset_id
            finally:
                host.close()
            application, host = create_agent_application(profile=profile, storage_configuration=configuration, environment={},
                                                        model=lambda request: None)
            try:
                recovered = application.get(actor, session['session_id'])
                self.assertEqual(recovered['scope_id'], 'independent-task')
                self.assertEqual(host.service('session.store').identity.dataset_id, dataset)
                self.assertFalse(any(spec.key.startswith('knowledge.') for spec in host.catalog.values()))
            finally:
                host.close()

    def test_root_application_accepts_arbitrary_consumer_owned_schema(self):
        records = StorageSlot('records', RELATIONAL_V1)

        def mount(ctx, config):
            database = require_storage(ctx, records)
            database.prepare(SchemaPackage('custom-tasks', (Migration(1,
                ('CREATE TABLE tasks(value TEXT PRIMARY KEY)',)),)))
            database.seal()
            ctx.provide('custom.tasks', database)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'task', 'records', 'custom-tasks')
            path = root / 'databases.json'
            path.write_text(json.dumps(configuration), encoding='utf-8')
            settings = Settings(root / 'originals', root / 'identities.json')
            custom = PluginSpec('custom.tasks', ('custom.tasks',), (records,), mount)
            with create_root_application(settings, profile={'version': 1, 'plugins': [
                    {'id': 'task', 'use': 'custom.tasks'}]}, environment={'KB_STORAGE_CONFIG': str(path)},
                    extra_plugins=(custom,)).host as host:
                with host.service('custom.tasks').transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('custom plugin owns this schema')")
                with host.service('custom.tasks').transaction(write=False) as tx:
                    self.assertEqual(tx.execute('SELECT value FROM tasks').scalar(), 'custom plugin owns this schema')

    def test_model_service_receives_budget_binding_and_independent_audit_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'provider', 'budget', 'model-budget', export='structured')
            path = root / 'databases.json'
            path.write_text(json.dumps(configuration), encoding='utf-8')
            profile = {'version': 1, 'plugins': [{'id': 'provider', 'use': 'models.service',
                'config': {'config_path': str(root / 'models.json'), 'audit_root': str(root / 'audit'),
                           'database_slot': 'budget'}}]}
            caps = {'model': 2, 'input_tokens': 100, 'output_tokens': 100}
            (root / 'models.json').write_text(json.dumps({'version': 1,
                'budget': {'path': str(root / 'unused.sqlite3'), 'caps': caps},
                'providers': {'local': {'adapter': 'openai-compatible',
                    'base_url': 'https://models.invalid/v1', 'api_key_env': 'KB_COMPOSITION_MODEL_KEY'}},
                'models': {'generation': {'provider': 'local', 'model': 'fixture',
                    'capabilities': ['chat'], 'reservation': caps}}}), encoding='utf-8')
            settings = Settings(root / 'originals', root / 'identities.json')
            with patch.dict('os.environ', {'KB_COMPOSITION_MODEL_KEY': 'synthetic-test-key'}):
                composition = create_root_application(settings, profile=profile,
                    environment={'KB_STORAGE_CONFIG': str(path)})
                try:
                    ledger = composition.host.service('models.budget')
                    self.assertEqual(ledger.database.identity.space.owner_id, 'model-budget')
                    self.assertFalse(hasattr(ledger.database, 'info'))
                    self.assertEqual(ledger.caps, caps)
                    self.assertFalse((root / 'unused.sqlite3').exists())
                    self.assertTrue((root / 'audit').is_dir())
                    self.assertEqual(set(composition.models.catalog()), {'generation'})
                finally:
                    composition.close()

    def test_external_provider_serves_knowledge_without_builtin_provider_registration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'records', owner='knowledge', export='structured')
            configuration['instances']['main']['provider'] = 'independent-supplier'
            opened = []

            def factory(config):
                opened.append(dict(config))
                return SqlStorageProvider(SQLiteProvider(config['root']))

            providers = {'independent-supplier': StorageProvider(factory, {
                'relational': (RELATIONAL_V1,), 'structured': (GUARDED_RELATIONAL_V1,)})}
            profile = {'version': 1, 'plugins': [{'id': 'records', 'use': 'platform.repository.storage'}]}
            settings = Settings(root, root / 'identities.json')
            with create_root_application(settings, profile=profile, environment={},
                    storage_configuration=configuration, provider_catalog=providers).host as host:
                repository = host.service('platform.repository')
                self.assertFalse(hasattr(repository.database, 'info'))
                platform = Platform(repository=repository, search=object(), store=object(),
                    parser=object(), embedding_jobs=object())
                actor = Actor('tenant', 'owner')
                created = platform.create_kb(actor, 'External supplier')
                self.assertEqual(platform.list_kbs(actor)[0]['kb_id'], created['kb_id'])
                self.assertEqual(len(opened), 1)

    def test_wrong_external_capability_is_rejected_before_provider_creation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'records', owner='knowledge', export='structured')
            configuration['instances']['main']['provider'] = 'independent-kv'
            configuration['services']['main.storage']['export'] = 'kv'
            opened = []
            providers = {'independent-kv': StorageProvider(lambda config: opened.append(config), {'kv': (KV_V1,)})}
            profile = {'version': 1, 'plugins': [{'id': 'records', 'use': 'platform.repository.storage'}]}
            with self.assertRaisesRegex(StorageError, 'STORAGE_CONTRACT_MISSING'):
                create_root_application(Settings(root, root / 'identities.json'), profile=profile,
                    environment={}, storage_configuration=configuration, provider_catalog=providers)
            self.assertEqual(opened, [])
