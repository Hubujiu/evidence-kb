from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
import json
import os
import unittest
from unittest.mock import patch

from kb_platform.configuration_diagnostics import configuration_diagnostics


class ConfigurationDiagnosticContracts(unittest.TestCase):
    def test_diagnostics_describe_selected_graph_without_exposing_config_values(self):
        application = SimpleNamespace(composition=SimpleNamespace(plan={'plugins': [
            {'id': 'llm', 'implementation': 'llm.service', 'version': 1,
                'config': {'api_key': 'PRIVATE-SECRET'}}]}),
            model_status=lambda: {'provider_id': 'synthetic', 'policy': {}, 'budget': {}})
        composition = SimpleNamespace(application=application,
            configuration_provenance={'configuration_sources': {'application': 'explicit',
                'storage': 'application_profile', 'models': 'explicit'},
                'inactive_environment': ['KB_AGENT_PROFILE']})
        result = configuration_diagnostics(composition)
        self.assertEqual(result['application']['kind'], 'contract')
        self.assertEqual(result['application']['nodes'], [{'id': 'llm', 'implementation': 'llm.service', 'version': 1}])
        self.assertEqual(result['inactive_environment'], ['KB_AGENT_PROFILE'])
        self.assertEqual(result['configuration_sources']['models'], 'explicit')
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['dependency_probe'], 'not_run')
        result['configuration_sources']['models'] = 'modified'
        self.assertEqual(configuration_diagnostics(composition)['configuration_sources']['models'], 'explicit')


class ConfigurationAssemblyDiagnosticContracts(unittest.TestCase):
    def test_owned_model_registry_does_not_report_unused_injected_registry(self):
        from kb_platform.application_composition import create_root_application

        ignored = object()
        composition = create_root_application(None, profile={'version': 1, 'plugins': [
            {'id': 'provider', 'use': 'models.service', 'config': {'owned': True}}]},
            environment={}, remote=False, models=ignored)
        self.addCleanup(composition.close)
        self.assertIsNot(composition.models, ignored)
        self.assertEqual(configuration_diagnostics(composition)['configuration_sources']['models'],
            'application_profile')

    def test_legacy_profile_records_generation_and_embedding_configuration_sources(self):
        from kb_platform.application_composition import CONFIG, configuration_provenance, load_application_profile

        for generation_override in (False, True):
            with self.subTest(generation_override=generation_override):
                environment = {'KB_PROVIDERS_CONFIG': 'synthetic-embedding-config'}
                if generation_override:
                    environment['KB_AGENT_PROVIDERS_CONFIG'] = 'synthetic-generation-config'
                provenance = {'sources': {}, 'used_environment': set()}
                selected = load_application_profile(None, profile=CONFIG / 'application-profiles/bm25.json',
                    remote=True, environment=environment, provenance=provenance)
                clients = next(row['config'] for row in selected['plugins'] if row['id'] == 'model-clients')
                self.assertEqual(clients['providers_config'], environment['KB_PROVIDERS_CONFIG'])
                self.assertEqual(clients['generation_config'], environment.get('KB_AGENT_PROVIDERS_CONFIG',
                    environment['KB_PROVIDERS_CONFIG']))
                result = configuration_provenance(provenance['sources'], environment, provenance['used_environment'])
                self.assertEqual(result['configuration_sources']['models'],
                    'KB_AGENT_PROVIDERS_CONFIG' if generation_override else 'KB_PROVIDERS_CONFIG')
                self.assertEqual(result['inactive_environment'], [])

        environment = {'KB_AGENT_PROFILE': str(CONFIG / 'agent-profiles/knowledge.json')}
        provenance = {'sources': {}, 'used_environment': set()}
        load_application_profile(None, remote=True, environment=environment, provenance=provenance)
        self.assertEqual(provenance['sources']['application'], 'KB_AGENT_PROFILE')
        self.assertIn('KB_AGENT_PROFILE', provenance['used_environment'])

    def test_borrowed_legacy_clients_do_not_report_ignored_files_as_active(self):
        from kb_platform.application_composition import CONFIG, create_root_application, load_application_profile
        from .contract_application_contracts import ContractApplicationContracts

        fixture = ContractApplicationContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        embedding = SimpleNamespace(dimension=2, manifest={'provider': 'synthetic'})
        environment = {'KB_PROVIDERS_CONFIG': '/ignored/embedding.json',
            'KB_AGENT_PROVIDERS_CONFIG': '/ignored/generation.json'}
        profile = load_application_profile(fixture.settings, profile=CONFIG / 'application-profiles/bm25.json',
            remote=True, environment=environment)
        profile['plugins'] = [row for row in profile['plugins'] if row['id'] in {
            'provider', 'model-clients', 'generation-route', 'embedding-route'}]
        environment.pop('KB_AGENT_PROVIDERS_CONFIG')
        composition = create_root_application(fixture.settings, profile=profile,
            remote=False, environment=environment, model_clients=(fixture.client, embedding))
        self.addCleanup(composition.close)
        result = configuration_diagnostics(composition)
        self.assertEqual(result['configuration_sources']['models'], 'explicit')
        self.assertEqual(result['inactive_environment'], ['KB_PROVIDERS_CONFIG'])
        self.assertEqual(fixture.physical, [])

    def test_http_diagnostics_report_profile_files_and_ignored_environment(self):
        from fastapi.testclient import TestClient
        from kb_platform.api import create_app
        from kb_platform.storage_catalog import initialize_configuration
        from .contract_application_contracts import ContractApplicationContracts

        fixture = ContractApplicationContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.storage['bindings']['provider'] = {'primary': {
            'service': 'records.domain', 'owner': 'provider', 'space': 'physical'}}
        storage = initialize_configuration(fixture.storage)
        storage_path = fixture.root / 'selected-storage.json'
        storage_path.write_text(json.dumps(storage), 'utf-8')
        models_path = fixture.root / 'selected-models.json'
        models_path.write_text(json.dumps({'version': 1,
            'budget': {'path': str(fixture.root / 'physical.sqlite3'), 'caps': {'requests': 2}},
            'providers': {'synthetic': {'adapter': 'openai-compatible',
                'base_url': 'https://synthetic.invalid/v1', 'api_key_env': 'MODEL_DIAGNOSTIC_FIXTURE_KEY'}},
            'models': {'generation': {'provider': 'synthetic', 'model': 'controlled',
                'capabilities': ['chat'], 'reservation': {'requests': 1}}}}), 'utf-8')
        fixture.profile['plugins']['storage']['config'] = {'path': str(storage_path)}
        fixture.profile['plugins']['llm']['config'] = {
            'config_path': str(models_path), 'budget_binding': 'provider.primary',
            'audit_root': str(fixture.root / 'audit')}
        environment = {'KB_DEFER_DEFAULT_APP': '1',
            'MODEL_DIAGNOSTIC_FIXTURE_KEY': 'synthetic-secret',
            'KB_APPLICATION_PROFILE': '/ignored/application.json',
            'KB_MODELS_CONFIG': '/ignored/models.json', 'KB_PROVIDERS_CONFIG': '/ignored/providers.json',
            'KB_STORAGE_CONFIG': '/ignored/storage.json'}
        profile_path = fixture.root / 'selected-profile.json'
        profile_path.write_text(json.dumps(fixture.profile), 'utf-8')
        for mode in ('explicit', 'environment', 'default', 'injected', 'configured_clients'):
            with self.subTest(mode=mode):
                selected_environment, options = dict(environment), {'profile': deepcopy(fixture.profile)}
                sources = {'application': 'explicit', 'models': 'application_profile', 'storage': 'application_profile'}
                inactive = {'KB_APPLICATION_PROFILE', 'KB_MODELS_CONFIG', 'KB_PROVIDERS_CONFIG', 'KB_STORAGE_CONFIG'}
                if mode == 'environment':
                    options = {}
                    selected_environment['KB_APPLICATION_PROFILE'] = str(profile_path)
                    sources['application'] = 'KB_APPLICATION_PROFILE'
                    inactive.remove('KB_APPLICATION_PROFILE')
                elif mode == 'default':
                    options = {}
                    selected_environment.update(KB_APPLICATION_PROFILE='', KB_REMOTE_MODELS='1',
                        KB_MODELS_CONFIG=str(models_path), KB_STORAGE_CONFIG=str(storage_path),
                        KB_RESEARCH_ROOT=str(fixture.root / 'default-research'))
                    sources = {'application': 'default', 'models': 'KB_MODELS_CONFIG', 'storage': 'KB_STORAGE_CONFIG'}
                    inactive = {'KB_PROVIDERS_CONFIG'}
                elif mode == 'injected':
                    options['profile']['plugins']['llm']['config'] = {}
                    options.update(models=fixture.models, storage_configuration=storage)
                    sources.update(models='explicit', storage='explicit')
                elif mode == 'configured_clients':
                    ignored_client = SimpleNamespace(**{**vars(fixture.client), 'provider_id': 'ignored-client'})
                    options['model_clients'] = (ignored_client, None)
                    profile = options['profile']
                    names = {name: 'selected-' + name for name in profile['plugins']}
                    profile['plugins'] = {names[name]: value for name, value in profile['plugins'].items()}
                    profile['bindings'] = {names[name]: {alias: {**target, 'plugin': names[target['plugin']]}
                        for alias, target in bindings.items()} for name, bindings in profile['bindings'].items()}
                    profile['entrypoint']['plugin'] = names[profile['entrypoint']['plugin']]
                template = (patch('kb_platform.application_knowledge.DEFAULT_APPLICATION', profile_path)
                    if mode == 'default' else nullcontext())
                with patch.dict('os.environ', selected_environment), template:
                    if mode == 'default':
                        os.environ.pop('KB_APPLICATION_PROFILE', None)
                    app = create_app(fixture.settings, **options)
                with TestClient(app) as client:
                    response = client.get('/diagnostics', headers={'Authorization': 'Bearer ' + fixture.token})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result['configuration_sources'], sources)
                self.assertTrue(inactive <= set(result['inactive_environment']))
                self.assertFalse(set(sources.values()) & set(result['inactive_environment']))
                self.assertEqual(result['model']['provider_id'], 'synthetic')
                self.assertEqual(result['model_calls'], 0)
                for private in ('/ignored/', str(fixture.root), 'synthetic-secret'):
                    self.assertNotIn(private, response.text)
