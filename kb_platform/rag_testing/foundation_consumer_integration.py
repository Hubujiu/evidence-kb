import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from .foundation_wire_fixture import LocalModelWire


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'examples/foundation-consumer/main.py'
PROGRAM = """import runpy
import sys
namespace = runpy.run_path(sys.argv[1])
namespace['main'](sys.argv[2:])
loaded = sorted(name for name in sys.modules if name.startswith('kb_platform.knowledge'))
if loaded:
    raise RuntimeError('FOUNDATION_IMPORTED_KNOWLEDGE: ' + ','.join(loaded))
"""


class FoundationConsumerIntegration(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def consumer(self, *arguments, environment=None):
        result = subprocess.run([sys.executable, '-c', PROGRAM, str(EXAMPLE), *map(str, arguments)],
            cwd=ROOT, env=dict(os.environ) if environment is None else environment,
            text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['implementations'], ['example.notes', 'foundation.llm', 'foundation.storage'])
        self.assertEqual(report['finish'], {'kind': 'stop'})
        return report

    def test_independent_example_persists_objects_and_kv_without_loading_knowledge(self):
        first = self.consumer('--root', self.root, '--message', 'first independent request')
        self.assertEqual(first['mode'], 'local-provider')
        self.assertEqual(first['reply'], 'Local provider received: first independent request')
        self.assertIsNone(first['usage'])
        self.assertIsNone(first['previous_reply'])
        second = self.consumer('--root', self.root, '--message', 'second independent request')
        self.assertEqual(second['previous_reply'], first['reply'])
        self.assertEqual(second['storage_identity'], first['storage_identity'])
        self.assertNotEqual(second['object']['sha256'], first['object']['sha256'])

    def test_independent_example_uses_configured_http_providers_through_the_same_consumer(self):
        for adapter in ('openai-compatible', 'anthropic'):
            with self.subTest(adapter=adapter):
                directory = self.root / adapter
                directory.mkdir()
                wire = LocalModelWire(adapter, ['Configured independent response'])
                self.addCleanup(wire.close)
                key = 'FOUNDATION_CHILD_KEY_' + uuid4().hex.upper()
                config = {'version': 1, 'budget': {
                    'path': str(directory / 'path-ledger.sqlite3'), 'caps': {'requests': 4}},
                    'providers': {'configured': {'adapter': adapter, 'base_url': wire.url + '/v1',
                        'api_key_env': key, 'timeout_seconds': 3}},
                    'models': {'generation': {'provider': 'configured', 'model': 'controlled-model',
                        'capabilities': ['chat'], 'reservation': {'requests': 1}}}}
                path = directory / 'models.json'
                path.write_text(json.dumps(config), 'utf-8')
                report = self.consumer('--root', directory, '--models-config', path,
                    '--message', 'A consumer with no knowledge application',
                    environment={**os.environ, key: 'local-child-fixture-key'})
                self.assertEqual(report['mode'], 'configured-provider')
                self.assertEqual(report['provider'], 'configured')
                self.assertEqual(report['reply'], 'Configured independent response')
                self.assertEqual(report['usage'], {'inputTokens': 10, 'outputTokens': 5, 'totalTokens': 15})
                self.assertEqual(len(wire.requests), 1)
                self.assertEqual(wire.errors, [])
