from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.application_composition import create_root_application
from kb_platform.application_knowledge import application_schemas
from kb_platform.plugin_deployment import load_plugin_catalog
from kb_platform.plugin_native import PluginError


class PluginDeploymentContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'configuration').mkdir()
        (self.root / 'workers').mkdir()
        self.path = self.root / 'configuration/catalog.json'
        self.document = {'version': 1, 'root': '../workers', 'definitions': [{
            'implementation': 'thirdparty.application', 'version': '1', 'executor': 'process',
            'command': ['worker', '--json-lines'],
            'provides': {'application': {'id': 'application.run', 'major': 1}}, 'requires': {}}],
            'schemas': [{'implementation': 'thirdparty.application', 'version': '1',
                'schema': {'type': 'object', 'additionalProperties': False}}]}

    def write(self, document):
        self.path.write_text(json.dumps(document), 'utf-8')

    def test_catalog_root_is_relative_to_catalog_and_preserves_exact_argv(self):
        self.write(self.document)
        selected = load_plugin_catalog(self.path)
        self.assertEqual(selected['process_root'], self.root / 'workers')
        self.assertEqual(selected['extra_catalog'], self.document['definitions'])
        self.assertEqual(selected['schemas'], {('thirdparty.application', '1'):
                                              self.document['schemas'][0]['schema']})

    def test_published_context_catalog_uses_the_actual_configuration_contract(self):
        path = Path(__file__).resolve().parents[2] / 'examples/contract-context/catalog.json'
        selected = load_plugin_catalog(path)
        self.assertEqual(selected['process_root'], path.parent)
        self.assertEqual(selected['extra_catalog'][0]['command'], ['python', 'worker.py'])
        self.assertEqual(selected['schemas'][('example.context.process', '1')],
                         application_schemas()[('knowledge.context', '1')])

    def test_catalog_rejects_local_execution_unknown_fields_and_unmatched_schemas(self):
        for variant in range(7):
            document = deepcopy(self.document)
            if variant == 0:
                document['definitions'][0]['executor'] = 'local'
            elif variant == 1:
                document['definitions'][0]['factory'] = 'arbitrary.module:mount'
            elif variant == 2:
                document['schemas'] = []
            elif variant == 3:
                document['schemas'].append(deepcopy(document['schemas'][0]))
            elif variant == 4:
                document['schemas'][0]['implementation'] = 'knowledge.context'
            elif variant == 5:
                document['definitions'].append(deepcopy(document['definitions'][0]))
            else:
                document['factory'] = 'arbitrary.module:mount'
            with self.subTest(variant=variant), self.assertRaises(PluginError):
                self.write(document)
                load_plugin_catalog(self.path)

    def test_catalog_schema_does_not_resolve_external_references(self):
        self.document['schemas'][0]['schema'] = {'$ref': 'https://invalid.example/schema'}
        self.write(self.document)
        with self.assertRaises(PluginError) as caught:
            load_plugin_catalog(self.path)
        self.assertEqual(caught.exception.code, 'CONTRACT_EXTERNAL_SCHEMA_REFERENCE')

    def test_root_rejects_bad_command_and_configuration_before_creating_host(self):
        settings = SimpleNamespace()
        profile = {'version': 1, 'plugins': {'entry': {
            'implementation': 'thirdparty.application', 'version': '1', 'config': {}}},
            'bindings': {}, 'entrypoint': {'plugin': 'entry', 'export': 'application'}}
        for invalid_command in (True, False):
            document, selected = deepcopy(self.document), deepcopy(profile)
            if invalid_command:
                document['definitions'][0]['command'] = []
            else:
                selected['plugins']['entry']['config']['undeclared'] = True
            self.write(document)
            with self.subTest(invalid_command=invalid_command), patch('kb_platform.plugin_contracts.PluginHost') as host:
                with self.assertRaises(PluginError) as caught:
                    create_root_application(settings, profile=selected,
                        environment={'KB_PLUGIN_CATALOG': str(self.path)})
                self.assertEqual(caught.exception.code,
                                 'invalid_composition' if invalid_command else 'INVALID_PLUGIN_CONFIG')
                host.assert_not_called()

    def test_catalog_cannot_be_silently_ignored_by_document_profile(self):
        self.write(self.document)
        with self.assertRaisesRegex(ValueError, 'PLUGIN_CATALOG_REQUIRES_CONTRACT_APPLICATION'):
            create_root_application(SimpleNamespace(), profile={'version': 1, 'plugins': []},
                                    environment={'KB_PLUGIN_CATALOG': str(self.path)})
