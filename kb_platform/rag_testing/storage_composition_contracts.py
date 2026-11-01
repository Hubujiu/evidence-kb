import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kb_platform.application_composition import create_root_application
from kb_platform.database_contracts import Migration, SchemaPackage
from kb_platform.database_plugins import require_database
from kb_platform.harness_profile import create_agent_application
from kb_platform.model_service import ModelService
from kb_platform.plugin_host import PluginSpec
from kb_platform.session_identity import Actor
from kb_platform.settings import Settings


class StorageCompositionContracts(unittest.TestCase):
    def configuration(self, root, consumer, slot='primary', owner='records'):
        return {'version': 1, 'application_id': 'standalone-project',
            'routes': {'main': {'provider': 'sqlite', 'root': str(root / 'databases')}},
            'bindings': {consumer: {slot: {'route': 'main', 'owner': owner, 'space': 'history'}}}}

    def test_standalone_agent_uses_configured_database_and_recovers_after_host_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'store')
            path = root / 'databases.json'
            path.write_text(json.dumps(configuration), encoding='utf-8')
            environment = {'KB_DATABASES_CONFIG': str(path)}
            actor = Actor('tenant', 'owner')
            application, host = create_agent_application(environment=environment, model=lambda request: None)
            try:
                session = application.create(actor, 'independent-task')
                dataset = host.service('session.store').identity.dataset_id
            finally:
                host.close()
            application, host = create_agent_application(database_configuration=configuration, environment={},
                                                        model=lambda request: None)
            try:
                recovered = application.get(actor, session['session_id'])
                self.assertEqual(recovered['scope_id'], 'independent-task')
                self.assertEqual(host.service('session.store').identity.dataset_id, dataset)
                self.assertFalse(any(spec.key.startswith('knowledge.') for spec in host.catalog.values()))
            finally:
                host.close()

    def test_root_application_accepts_arbitrary_consumer_owned_schema(self):
        def mount(ctx, config):
            database = require_database(ctx, 'records')
            database.prepare(SchemaPackage('custom-tasks', (Migration(1,
                ('CREATE TABLE tasks(value TEXT PRIMARY KEY)',)),)))
            database.seal()
            ctx.provide('custom.tasks', database)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'task', 'records', 'custom-tasks')
            path = root / 'databases.json'
            path.write_text(json.dumps(configuration), encoding='utf-8')
            settings = Settings('', '', root / 'originals', root / 'identities.json', 'postgres')
            custom = PluginSpec('custom.tasks', ('custom.tasks',), (), mount, storage_slots=('records',))
            with create_root_application(settings, profile={'version': 1, 'plugins': [
                    {'id': 'task', 'use': 'custom.tasks'}]}, environment={'KB_DATABASES_CONFIG': str(path)},
                    extra_plugins=(custom,)).host as host:
                with host.service('custom.tasks').transaction() as tx:
                    tx.execute("INSERT INTO tasks VALUES('custom plugin owns this schema')")
                with host.service('custom.tasks').transaction(write=False) as tx:
                    self.assertEqual(tx.execute('SELECT value FROM tasks').scalar(), 'custom plugin owns this schema')

    def test_model_service_receives_budget_binding_and_independent_audit_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self.configuration(root, 'provider', 'budget', 'model-budget')
            path = root / 'databases.json'
            path.write_text(json.dumps(configuration), encoding='utf-8')
            profile = {'version': 1, 'plugins': [{'id': 'provider', 'use': 'models.service',
                'config': {'config_path': str(root / 'models.json'), 'audit_root': str(root / 'audit')}}]}
            models = ModelService()
            self.addCleanup(models.close)
            settings = Settings('', '', root / 'originals', root / 'identities.json', 'postgres')
            with patch('kb_platform.model_configuration.read_model_config', return_value={'providers': {}}), \
                    patch('kb_platform.model_plugins.file_inputs', return_value={}), \
                    patch('kb_platform.model_configuration.from_config', return_value=models) as factory:
                composition = create_root_application(settings, profile=profile,
                    environment={'KB_DATABASES_CONFIG': str(path)})
                try:
                    self.assertIs(composition.models, models)
                    self.assertEqual(factory.call_args.kwargs['audit_root'], str(root / 'audit'))
                    database = factory.call_args.kwargs['database']
                    self.assertEqual(database.identity.space.owner_id, 'model-budget')
                    self.assertEqual(database.info.engine, 'sqlite')
                finally:
                    composition.close()
