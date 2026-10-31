from copy import deepcopy
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.harness_services import builtin_plugins
from kb_platform.knowledge_profile import knowledge_plugins
from kb_platform.model_plugins import model_plugins
from kb_platform.platform_plugins import platform_plugins
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError
from kb_platform.settings import Settings


class PluginConfigurationContracts(unittest.TestCase):
    def test_integral_float_is_rejected_before_replacing_integer_policy(self):
        profile = {'version': 1, 'plugins': [{'id': 'executor', 'use': 'knowledge.executor',
            'config': {'max_attempts': 2}}]}
        with PluginHost(profile, knowledge_plugins(None, None)) as host:
            before = host.inspect()
            with self.assertRaises(PluginError):
                host.update('executor', config={'max_attempts': 2.0})
            self.assertEqual(host.inspect()['instances'], before['instances'])
            self.assertEqual(host.inspect()['desired_revision'], before['desired_revision'])

    def model_config(self, root):
        return {'version': 1,
            'budget': {'path': str(root / 'new-ledger' / 'physical.sqlite3'), 'caps': {'requests': 5}},
            'providers': {'fixture': {'adapter': 'openai-compatible', 'base_url': 'https://fixture.invalid/v1',
                                       'api_key_env': 'C1_MODEL_SECRET'}},
            'models': {'generation': {'provider': 'fixture', 'model': 'first', 'capabilities': ['chat'],
                                       'reservation': {'requests': 1}}}}

    def test_all_builtin_specs_declare_strict_object_configuration(self):
        settings = Settings('', '', Path('.'), Path('unused'))
        specs = (*builtin_plugins(), *model_plugins(), *platform_plugins(settings),
                 *knowledge_plugins(None, None))
        for spec in specs:
            with self.subTest(plugin=spec.key):
                self.assertIsNotNone(spec.config_schema)
                self.assertEqual(spec.config_schema['type'], 'object')
                self.assertFalse(spec.config_schema['additionalProperties'])

    def test_invalid_store_apply_and_update_preserve_live_registration_and_revision(self):
        with TemporaryDirectory() as directory:
            profile = {'version': 1, 'plugins': [
                {'id': 'store', 'use': 'harness.store.sqlite', 'config': {'root': directory}}]}
            with PluginHost(profile, builtin_plugins()) as host:
                before, binding = host.inspect(), host.bind('session.store')
                original = host.service('session.store')
                for config in ({'root': 7}, {'root': directory, 'rot': directory}):
                    with self.subTest(config=config):
                        candidate = deepcopy(profile)
                        candidate['plugins'][0]['config'] = config
                        with self.assertRaises(PluginError):
                            host.apply(candidate)
                        with self.assertRaises(PluginError):
                            host.update('store', config=config)
                        after = host.inspect()
                        self.assertEqual(after['desired_revision'], before['desired_revision'])
                        self.assertEqual(after['observed_revision'], before['observed_revision'])
                        self.assertEqual(after['instances'], before['instances'])
                        self.assertEqual(host.bind('session.store').identity, binding.identity)
                        self.assertIs(host.service('session.store'), original)

    def test_invalid_context_relation_rejects_before_any_live_change(self):
        profile = {'version': 1, 'plugins': [
            {'id': 'policy', 'use': 'harness.context.policy',
             'config': {'input_budget': 1000, 'model_window': 4096, 'output_reserve': 512}}]}
        with PluginHost(profile, builtin_plugins()) as host:
            before, binding = host.inspect(), host.bind('context.policy')
            with self.assertRaises((PluginError, ValueError)):
                host.update('policy', config={'model_window': 100, 'output_reserve': 80, 'safety_reserve': 30})
            self.assertEqual(host.inspect()['instances'], before['instances'])
            self.assertEqual(host.bind('context.policy').identity, binding.identity)

    def test_unknown_platform_configuration_has_no_constructor_effects(self):
        settings = Settings('', '', Path('.'), Path('unused'))
        profile = {'version': 1, 'plugins': [
            {'id': 'originals', 'use': 'platform.originals.files', 'config': {'rot': 'must-not-create'}}]}
        with patch('kb_platform.storage.OriginalStore', side_effect=AssertionError('must not construct')):
            with self.assertRaises(PluginError):
                PluginHost(profile, platform_plugins(settings))

    def test_invalid_model_route_capability_does_not_revoke_live_route(self):
        client = SimpleNamespace(provider_id='fixture', model='fixed')
        profile = {'version': 1, 'plugins': [
            {'id': 'models', 'use': 'models.service'}, {'id': 'clients', 'use': 'models.clients'},
            {'id': 'generation', 'use': 'models.route.generation',
             'config': {'route': 'generation', 'client': 'generation', 'capability': 'chat'}}]}
        with PluginHost(profile, model_plugins(clients={'generation': client})) as host:
            registry = host.service('models')
            before = host.inspect()
            with self.assertRaises(PluginError):
                host.update('generation', config={'route': 'generation', 'client': 'generation',
                                                  'capability': 'image'})
            self.assertEqual(host.inspect()['instances'], before['instances'])
            self.assertIn('generation', registry.catalog())

    def test_invalid_referenced_model_configuration_has_no_live_or_persistent_effect(self):
        with TemporaryDirectory() as directory, patch.dict('os.environ', {'C1_MODEL_SECRET': 'synthetic-private'}):
            root = Path(directory)
            path = root / 'models.json'
            profile = {'version': 1, 'plugins': [{'id': 'models', 'use': 'models.service'}]}
            with PluginHost(profile, model_plugins()) as host:
                before, binding = host.inspect(), host.bind('models')
                registry = host.service('models')
                variants = ['{']
                original = self.model_config(root)
                for mutate in (
                    lambda value: value.update(unknown=True),
                    lambda value: value['models']['generation'].update(capabilities=['unsupported']),
                    lambda value: value['models']['generation'].update(unknown=True),
                    lambda value: value['providers']['fixture'].update(base_url='https://secret:token@fixture.invalid/v1'),
                    lambda value: value['providers']['fixture'].update(base_url='https://fixture.invalid:notaport'),
                    lambda value: value['providers']['fixture'].update(base_url='https://fixture.invalid/v1\npath'),
                    lambda value: value['providers']['fixture'].update(base_url='https://fixture.invalid:65536/v1'),
                    lambda value: value['models']['generation'].update(reservation={'different': 1}),
                    lambda value: value['models']['generation'].update(reservation={'requests': 6}),
                    lambda value: value['providers']['fixture'].update(adapter='unsupported'),
                ):
                    changed = deepcopy(original)
                    mutate(changed)
                    variants.append(json.dumps(changed))
                for content in variants:
                    with self.subTest(content=content):
                        path.write_text(content, 'utf-8')
                        candidate = deepcopy(profile)
                        candidate['plugins'][0]['config'] = {'config_path': str(path)}
                        with self.assertRaises((PluginError, ValueError)):
                            host.apply(candidate)
                        with self.assertRaises((PluginError, ValueError)):
                            host.update('models', config={'config_path': str(path)})
                        after = host.inspect()
                        self.assertEqual(after['instances'], before['instances'])
                        self.assertEqual(after['desired_revision'], before['desired_revision'])
                        self.assertEqual(after['observed_revision'], before['observed_revision'])
                        self.assertEqual(host.bind('models').identity, binding.identity)
                        self.assertIs(host.service('models'), registry)
                        self.assertFalse((root / 'new-ledger').exists())

    def test_changed_referenced_file_reloads_same_path_and_exposes_only_fingerprints(self):
        with TemporaryDirectory() as directory, patch.dict('os.environ', {'C1_MODEL_SECRET': 'synthetic-private'}):
            root = Path(directory)
            path = root / 'models.json'
            value = self.model_config(root)
            path.write_text(json.dumps(value), 'utf-8')
            profile = {'version': 1, 'plugins': [
                {'id': 'models', 'use': 'models.service', 'config': {'config_path': str(path)}}]}
            with PluginHost(profile, model_plugins()) as host:
                before = host.bind('models')
                value['models']['generation']['model'] = 'second'
                path.write_text(json.dumps(value), 'utf-8')
                host.apply(profile)
                self.assertNotEqual(host.bind('models').identity, before.identity)
                self.assertEqual(host.service('models').catalog()['generation'].model, 'second')
                self.assertNotIn('synthetic-private', json.dumps(host.inspect()))
                with self.assertRaises(PluginError):
                    before.call('catalog')

    def test_changed_credential_reloads_same_file_and_exposes_only_credential_hash(self):
        from hashlib import sha256
        import os
        from kb_platform.model_http import OpenAICompatibleAdapter

        credentials = []
        def adapter(**options):
            credentials.append(options['api_key'])
            return OpenAICompatibleAdapter(**options)

        with TemporaryDirectory() as directory, patch.dict('os.environ', {'C1_MODEL_SECRET': 'first-private'}):
            path = Path(directory) / 'models.json'
            path.write_text(json.dumps(self.model_config(Path(directory))), 'utf-8')
            profile = {'version': 1, 'plugins': [
                {'id': 'models', 'use': 'models.service', 'config': {'config_path': str(path)}}]}
            with patch('kb_platform.model_http.OpenAICompatibleAdapter', side_effect=adapter):
                with PluginHost(profile, model_plugins()) as host:
                    before = host.bind('models')
                    host.apply(profile)
                    self.assertEqual(host.bind('models').identity, before.identity)
                    os.environ['C1_MODEL_SECRET'] = 'second-private'
                    host.apply(profile)
                    self.assertNotEqual(host.bind('models').identity, before.identity)
                    self.assertEqual(credentials, ['first-private', 'second-private'])
                    state = json.dumps(host.inspect())
                    self.assertIn('env:C1_MODEL_SECRET', state)
                    self.assertIn(sha256(b'second-private').hexdigest(), state)
                    self.assertNotIn('first-private', state)
                    self.assertNotIn('second-private', state)

    def test_invalid_referenced_remote_provider_does_not_close_existing_clients(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'providers.json'
            config = {'luna': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture', 'api_key': 'synthetic'},
                'google': {'base_url': 'https://fixture.invalid/v1', 'model': 'gemini-embedding-2',
                           'api_key': 'synthetic', 'dimension': 768},
                'cache_dir': str(root / 'cache'), 'log_path': str(root / 'calls.jsonl')}
            path.write_text(json.dumps(config), 'utf-8')
            settings = Settings('', '', root, root / 'unused')
            profile = {'version': 1, 'plugins': [{'id': 'clients', 'use': 'platform.model_clients.remote',
                                                'config': {'providers_config': str(path)}}]}
            with PluginHost(profile, platform_plugins(settings)) as host:
                before, binding = host.inspect(), host.bind('model.clients')
                clients = host.service('model.clients')
                config['google']['dimension'] = 2
                path.write_text(json.dumps(config), 'utf-8')
                with self.assertRaises((PluginError, ValueError, RuntimeError)):
                    host.apply(profile)
                self.assertEqual(host.inspect()['instances'], before['instances'])
                self.assertEqual(host.bind('model.clients').identity, binding.identity)
                self.assertIs(host.service('model.clients'), clients)
                self.assertFalse(clients['generation'].client.is_closed)

    def test_semantic_reference_preflight_reads_without_creating_provider_ledger_or_store(self):
        from kb_platform.google_semantic_provider import semantic_provider_manifest
        from kb_platform.rag_testing.contracts import digest
        from kb_platform.remote import google_embedding_manifest
        from kb_platform.semantic_page_runtime import read_semantic_config

        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider_path, path = root / 'provider.json', root / 'semantic.json'
            provider = {'base_url': 'https://fixture.invalid/v1', 'model': 'gemini-embedding-2',
                        'api_key': 'synthetic', 'dimension': 768}
            provider_path.write_text(json.dumps({'google': provider, 'cache_dir': str(root / 'effects/cache'),
                                                'log_path': str(root / 'effects/calls.jsonl')}), 'utf-8')
            remote = google_embedding_manifest(provider)
            config = {'schema': 'semantic-page-runtime/2', 'provider_id': 'google_gemini',
                'storage_root': str(root / 'effects/store'), 'ledger_path': str(root / 'effects/ledger.sqlite3'),
                'audit_dir': str(root / 'effects/audit'), 'provider_config': str(provider_path),
                'caps': {'remote_embedding_calls': 2, 'input_tokens': 1000},
                'expected_provider_manifest_sha256': digest(semantic_provider_manifest(remote, 768))}
            path.write_text(json.dumps(config), 'utf-8')
            read_semantic_config(path)
            self.assertFalse((root / 'effects').exists())

            def resources(ctx, config):
                ctx.provide('knowledge.platform', None)
                ctx.provide('knowledge.gateway', None)

            profile = {'version': 1, 'plugins': [{'id': 'resources', 'use': 'fixture.resources'},
                                                {'id': 'semantic', 'use': 'knowledge.semantic'}]}
            catalog = (*knowledge_plugins(None, None), PluginSpec('fixture.resources',
                       ('knowledge.platform', 'knowledge.gateway'), (), resources))
            with PluginHost(profile, catalog) as host:
                before = host.inspect()
                config['caps']['remote_embedding_calls'] = True
                path.write_text(json.dumps(config), 'utf-8')
                with patch('kb_platform.semantic_page_runtime.GoogleSemanticProvider',
                           side_effect=AssertionError('preflight cannot create a provider')):
                    with self.assertRaises((PluginError, ValueError, RuntimeError)):
                        host.update('semantic', config={'config_path': str(path)})
                self.assertEqual(host.inspect()['instances'], before['instances'])
                self.assertIsNone(host.service('knowledge.semantic'))
                self.assertFalse((root / 'effects').exists())


if __name__ == '__main__':
    unittest.main()
