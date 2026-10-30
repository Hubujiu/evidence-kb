import json
from pathlib import Path
import tempfile
import unittest

from kb_platform.plugin_native import PluginError
from kb_platform.plugin_process import PROTOCOL, ProcessPackages


class ProcessPackageContracts(unittest.TestCase):
    def test_install_is_content_addressed_and_catalog_does_not_execute_package(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source'
            source.mkdir()
            (source / 'plugin.py').write_text('raise RuntimeError("must not execute at install")', 'utf-8')
            (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
                'version': '1', 'entry': 'plugin.py', 'services': {'echo': ['invoke']}}), 'utf-8')
            manager = ProcessPackages(Path(root) / 'installed')
            installed = manager.install(source)
            self.assertEqual(installed, manager.install(source))
            spec = manager.catalog(installed['package'], journal=Path(root) / 'journal')
            self.assertEqual(spec.provides, ('echo',))
            self.assertEqual(spec.key, 'process.fixture')
            (source / 'plugin.py').write_text('raise RuntimeError("new version")', 'utf-8')
            self.assertNotEqual(installed['package'], manager.install(source)['package'])
            self.assertEqual(len(manager.catalogs(journal=Path(root) / 'journal')), 1)
            (Path(installed['package']) / 'plugin.py').write_text('modified after install', 'utf-8')
            with self.assertRaises(PluginError) as failure:
                manager.catalog(installed['package'], journal=Path(root) / 'journal')
            self.assertEqual(failure.exception.code, 'PROCESS_PACKAGE_CONTENT_CHANGED')

    def test_manifest_rejects_entry_outside_package_before_effects(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source'
            source.mkdir()
            (Path(root) / 'outside.py').write_text('', 'utf-8')
            (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
                'version': '1', 'entry': '../outside.py', 'services': {'echo': ['invoke']}}), 'utf-8')
            manager = ProcessPackages(Path(root) / 'installed')
            with self.assertRaises(PluginError) as failure:
                manager.install(source)
            self.assertEqual(failure.exception.code, 'INVALID_PROCESS_ENTRY')
            self.assertEqual(list(manager.root.iterdir()), [])
