import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kb_platform.application_composition import create_root_application, load_application_profile
from kb_platform.model_errors import RemoteModelError
from kb_platform.model_service import Capability, ModelInfo, ModelService
from kb_platform.settings import Settings

from . import contract_application_contracts


class ProfileSelected(Exception):
    pass


class ApplicationRoutingContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root / 'store', self.root / 'identities.json', self.root / 'storage.json')

    def environment(self, routes):
        configuration = {'version': 1, 'budget': {'path': str(self.root / 'budget.sqlite3'),
            'caps': {'requests': 10}}, 'providers': {'controlled': {'adapter': 'openai-compatible',
            'base_url': 'https://controlled.invalid/v1', 'api_key_env': 'SYNTHETIC_MODEL_KEY'}},
            'models': {route: {'provider': 'controlled', 'model': 'controlled', 'reservation': {'requests': 1},
                'capabilities': ['chat' if route == 'generation' else route],
                **({'dimension': 2} if route == 'embedding' else {})} for route in routes}}
        path = self.root / 'models.json'
        path.write_text(json.dumps(configuration), 'utf-8')
        return {'KB_REMOTE_MODELS': '1', 'KB_MODELS_CONFIG': str(path)}

    def test_default_document_model_routes_keep_document_profile_without_generation(self):
        for routes in [('embedding',), ('ocr',), ('embedding', 'ocr')]:
            with self.subTest(routes=routes):
                environment, selected = self.environment(routes), []

                def observe(*args, **kwargs):
                    selected.append(load_application_profile(*args, **kwargs))
                    raise ProfileSelected()

                with patch('kb_platform.application_composition.load_application_profile', side_effect=observe), \
                        patch('kb_platform.application_knowledge.create_contract_knowledge',
                              side_effect=AssertionError('Document routes must not require generation')), \
                        self.assertRaises(ProfileSelected):
                    create_root_application(self.settings, environment=environment)
                plugins = {row['id']: row for row in selected[0]['plugins']}
                self.assertEqual(plugins['provider']['config']['config_path'], environment['KB_MODELS_CONFIG'])
                self.assertEqual('embedding' in plugins, 'embedding' in routes)
                self.assertEqual('ocr' in plugins, 'ocr' in routes)
                expected = 'models' if len(routes) == 2 else routes[0]
                self.assertEqual(plugins['capabilities']['use'], 'platform.capabilities.' + expected)
                self.assertFalse(any(row['use'].startswith(('knowledge.', 'harness.'))
                                     for row in plugins.values()))

    def test_default_generation_route_selects_contract_application(self):
        for routes in [('generation',), ('generation', 'embedding')]:
            with self.subTest(routes=routes):
                environment = self.environment(routes)
                selected = object()
                with patch('kb_platform.application_knowledge.create_contract_knowledge',
                           return_value=selected) as create, \
                        patch('kb_platform.application_composition.load_application_profile',
                              side_effect=AssertionError('Generation must use the contract application')):
                    self.assertIs(create_root_application(self.settings, environment=environment), selected)
                create.assert_called_once()
                self.assertIsNone(create.call_args.kwargs['profile'])
                self.assertEqual(create.call_args.kwargs['environment'], environment)

    def test_explicit_contract_application_keeps_generation_requirement_with_document_models(self):
        fixture = contract_application_contracts.ContractApplicationContracts()
        fixture.setUp()
        models = ModelService()
        self.addCleanup(models.close)

        class Embedding:
            info = ModelInfo('controlled', 'embedding', frozenset({Capability.EMBEDDING}))

            def invoke(inner, request):
                self.fail('Routing validation must not invoke a model')

        models.register('embedding', Embedding())
        try:
            with patch('kb_platform.application_composition.load_application_profile',
                       side_effect=AssertionError('Explicit contract maps cannot fall back to documents')), \
                    self.assertRaises(RemoteModelError) as caught:
                create_root_application(fixture.settings, profile=fixture.profile,
                    environment=self.environment(('embedding',)), models=models,
                    storage_configuration=fixture.storage)
            self.assertEqual(caught.exception.code, 'MODEL_ROUTE_UNAVAILABLE')
            self.assertEqual(set(models.catalog()), {'embedding'})
        finally:
            fixture.doCleanups()
