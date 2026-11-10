from types import SimpleNamespace
import unittest

from kb_platform.configuration_diagnostics import configuration_diagnostics


class ConfigurationDiagnosticContracts(unittest.TestCase):
    def test_diagnostics_describe_selected_graph_without_exposing_config_values(self):
        application = SimpleNamespace(composition=SimpleNamespace(plan={'plugins': [
            {'id': 'llm', 'implementation': 'llm.service', 'version': 1,
                'config': {'api_key': 'PRIVATE-SECRET'}}]}),
            model_status=lambda: {'provider_id': 'synthetic', 'policy': {}, 'budget': {}})
        composition = SimpleNamespace(application=application)
        result = configuration_diagnostics(composition,
            environment={'KB_AGENT_PROFILE': 'PRIVATE-PATH', 'KB_MODELS_CONFIG': 'PRIVATE-MODEL-PATH'},
            explicit_profile=True, explicit_models=True)
        self.assertEqual(result['application']['kind'], 'contract')
        self.assertEqual(result['application']['nodes'], [{'id': 'llm', 'implementation': 'llm.service', 'version': 1}])
        self.assertEqual(result['inactive_environment'], ['KB_AGENT_PROFILE'])
        self.assertEqual(result['configuration_sources']['models'], 'explicit')
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['dependency_probe'], 'not_run')
