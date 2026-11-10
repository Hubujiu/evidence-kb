from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import httpx

from kb_platform.model_configuration import from_config, read_model_config
from kb_platform.model_http import OpenAICompatibleAdapter
from kb_platform.model_plugins import model_plugins
from kb_platform.model_provider_plugins import compile_model_plugins
from kb_platform.model_provider_registry import ProviderAdapterRegistry, register_provider_adapter
from kb_platform.model_sdk import Capability, ModelRequest
from kb_platform.plugin_configuration import config_schema
from kb_platform.plugin_host import PluginHost


class ModelProviderRegistryContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = ProviderAdapterRegistry()
        self.path = self.root / 'models.json'
        self.config = {'version': 1,
            'budget': {'path': str(self.root / 'physical.sqlite3'), 'caps': {'requests': 2}},
            'providers': {'synthetic': {'adapter': 'trusted-fixture', 'base_url': 'https://fixture.invalid/v1',
                'api_key_env': 'MODEL_REGISTRY_FIXTURE_KEY', 'options': {'revision': 'synthetic-v1'}}},
            'models': {'generation': {'provider': 'synthetic', 'model': 'fixture',
                'capabilities': ['chat'], 'reservation': {'requests': 1}}}}
        self.environment = patch.dict('os.environ', {'MODEL_REGISTRY_FIXTURE_KEY': 'synthetic-secret'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def register(self, factory):
        self.registry.register('trusted-fixture', capabilities=[Capability.CHAT], factory=factory,
            options_schema=config_schema({'revision': {'const': 'synthetic-v1'}}, required=('revision',)))

    def write(self):
        self.path.write_text(json.dumps(self.config), 'utf-8')

    def test_trusted_extension_runs_through_standard_profile_without_new_enum_or_factory_branch(self):
        sent, factories = [], []

        def factory(*, provider_options, **options):
            factories.append(deepcopy(provider_options))
            return OpenAICompatibleAdapter(**options)

        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'synthetic answer'}}]})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client, \
                patch('kb_platform.model_configuration.PROVIDER_ADAPTERS', self.registry), \
                patch('kb_platform.model_provider_registry.PROVIDER_ADAPTERS', self.registry):
            register_provider_adapter('trusted-fixture', capabilities=[Capability.CHAT], factory=factory,
                options_schema=config_schema({'revision': {'const': 'synthetic-v1'}}, required=('revision',)))
            self.write()
            profile = {'version': 1, 'plugins': [{'id': 'models', 'use': 'models.service',
                                                'config': {'config_path': str(self.path)}}]}
            profile, catalog = compile_model_plugins(profile, model_plugins())
            with patch('kb_platform.model_provider_plugins.httpx.Client', return_value=client), \
                    PluginHost(profile, catalog) as host:
                model = host.service('models')
                result = model.invoke('generation', ModelRequest(Capability.CHAT,
                    {'messages': [{'role': 'user', 'content': 'Synthetic registry fixture'}]}))
                self.assertEqual(result.data['text'], 'synthetic answer')
                self.assertEqual(host.service('models.budget').summary()['physical_requests'], 1)
                self.assertNotIn('synthetic-secret', repr(model.catalog()))
        self.assertEqual(factories, [{'revision': 'synthetic-v1'}])
        self.assertEqual(len(sent), 1)

    def test_schema_capability_and_unregistered_import_paths_fail_before_factory_or_budget(self):
        factory = Mock()
        self.register(factory)
        original = deepcopy(self.config)
        changes = [
            lambda: self.config['providers']['synthetic'].update(adapter='os.system'),
            lambda: self.config['providers']['synthetic'].update(options={'revision': 'invalid'}),
            lambda: self.config['providers']['synthetic'].update(options={'revision': 'synthetic-v1', 'extra': True}),
            lambda: self.config['providers']['synthetic'].pop('options'),
            lambda: self.config['models']['generation'].update(capabilities=['embedding']),
        ]
        for change in changes:
            self.config = deepcopy(original)
            change()
            self.write()
            with self.subTest(config=self.config), self.assertRaises(ValueError):
                read_model_config(self.path, adapters=self.registry)
        factory.assert_not_called()
        self.assertFalse((self.root / 'physical.sqlite3').exists())

    def test_registry_rejects_replacement_and_returns_detached_schema(self):
        factory = Mock()
        self.register(factory)
        with self.assertRaisesRegex(ValueError, 'PROVIDER_ADAPTER_ALREADY_REGISTERED'):
            self.register(Mock())
        definition = self.registry.definition('trusted-fixture')
        definition.options_schema['properties'].clear()
        schema = self.registry.provider_schema()
        schema['oneOf'][0]['properties']['options']['properties'].clear()
        self.write()
        self.assertEqual(read_model_config(self.path, adapters=self.registry), self.config)
        self.assertIs(self.registry.definition('trusted-fixture').factory, factory)

    def test_factory_identity_mismatch_closes_adapter_and_does_not_register_route(self):
        created = []

        def factory(*, provider_options, **options):
            options['model'] = 'wrong-model'
            adapter = OpenAICompatibleAdapter(**options)
            adapter.close = Mock(wraps=adapter.close)
            created.append(adapter)
            return adapter

        self.register(factory)
        self.write()
        with self.assertRaisesRegex(ValueError, 'MODEL_ADAPTER_IDENTITY_MISMATCH'):
            from_config(self.path, adapters=self.registry)
        self.assertEqual(len(created), 1)
        created[0].close.assert_called_once_with()
