from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import bootstrap
from kb_platform.application_composition import create_root_application
from kb_platform.core import Actor
from kb_platform.settings import Settings


class StorageBootstrapContracts(unittest.TestCase):
    def test_explicit_bootstrap_preserves_identities_and_data_on_repeat(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            copyfile(Path(__file__).resolve().parents[2] / 'config/storage.local.example.json',
                     root / 'config/storage.local.example.json')
            with patch.object(bootstrap, 'ROOT', root), redirect_stdout(StringIO()):
                bootstrap.main([])
            storage = root / '.local/storage.json'
            identities = root / '.local/identities.json'
            before = (storage.read_bytes(), identities.read_bytes())
            configuration = json.loads(before[0])
            actor = Actor('synthetic-bootstrap-tenant', 'owner')
            settings = Settings(root / 'originals', identities)
            with create_root_application(settings, environment={}, storage_configuration=configuration).host as host:
                scope = host.call('platform', 'create_kb', actor, 'Synthetic bootstrap')['kb_id']
                host.call('platform', 'ingest', actor, scope, 'fixture.md', b'QUARTZMARKER review before release.')
                build = host.call('platform', 'stage', actor, scope, ['bm25'])
                host.call('platform', 'publish', actor, scope, build['build_id'])
            with patch.object(bootstrap, 'ROOT', root), redirect_stdout(StringIO()):
                bootstrap.main([])
            self.assertEqual(before, (storage.read_bytes(), identities.read_bytes()))
            with create_root_application(settings, environment={}, storage_configuration=configuration).host as host:
                result = host.call('platform', 'search', actor, scope, 'QUARTZMARKER')
                self.assertEqual(result['context'], 'QUARTZMARKER review before release.')
                self.assertEqual(result['manifest_id'], build['build_id'])

    def test_knowledge_initialization_creates_explicit_session_and_budget_spaces(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            copyfile(Path(__file__).resolve().parents[2] / 'config/storage.local.example.json',
                     root / 'config/storage.local.example.json')
            with patch.object(bootstrap, 'ROOT', root), redirect_stdout(StringIO()):
                bootstrap.main(['--application', 'knowledge'])
            path = root / '.local/storage-knowledge.json'
            before = path.read_bytes()
            value = json.loads(before)
            self.assertEqual(value['application_id'], 'knowledge-application-v4')
            self.assertNotEqual(value['bindings']['store']['primary']['dataset_id'],
                                value['bindings']['provider']['primary']['dataset_id'])
            with patch.object(bootstrap, 'ROOT', root), redirect_stdout(StringIO()):
                bootstrap.main(['--application', 'knowledge'])
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((root / '.local/storage.json').exists())
