from concurrent.futures import Future
from io import BytesIO
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError
from kb_platform.plugin_process import PROTOCOL, ProcessPackages, ProcessTransport, _Write


class ProcessPackageContracts(unittest.TestCase):
    def package(self, root):
        source = Path(root) / 'source'
        source.mkdir()
        (source / 'plugin.py').write_text('from .helper import value\n', 'utf-8')
        (source / 'helper.py').write_text('value = "pinned"\n', 'utf-8')
        (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
            'version': '1', 'entry': 'plugin.py', 'services': {'echo': ['invoke']}}), 'utf-8')
        manager = ProcessPackages(Path(root) / 'installed')
        return manager, manager.install(source)

    def test_added_package_can_not_shadow_pinned_helper_module(self):
        with tempfile.TemporaryDirectory() as root:
            manager, installed = self.package(root)
            shadow = Path(installed['package']) / 'helper'
            shadow.mkdir()
            (shadow / '__init__.py').write_text('value = "shadow"\n', 'utf-8')
            with self.assertRaises(PluginError) as failure:
                manager.catalog(installed['package'], journal=Path(root) / 'journal')
            self.assertEqual(failure.exception.code, 'PROCESS_PACKAGE_CONTENT_CHANGED')

    def test_mount_rechecks_files_changed_after_catalog_creation(self):
        with tempfile.TemporaryDirectory() as root:
            manager, installed = self.package(root)
            spec = manager.catalog(installed['package'], journal=Path(root) / 'journal')
            (Path(installed['package']) / 'helper.py').write_text('value = "changed"\n', 'utf-8')
            with patch('kb_platform.plugin_process.ProcessTransport.start',
                       side_effect=AssertionError('changed code must not execute')) as start:
                with self.assertRaises(PluginError) as failure:
                    PluginHost({'version': 1, 'plugins': [{'id': 'fixture', 'use': spec.key}]}, [spec])
                self.assertEqual(failure.exception.code, 'PROCESS_PACKAGE_CONTENT_CHANGED')
                start.assert_not_called()

    def test_source_bytecode_is_not_installed_and_installed_bytecode_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source'
            source.mkdir()
            (source / '__pycache__').mkdir()
            (source / '__pycache__/helper.cpython-312.pyc').write_bytes(b'untrusted stale bytecode')
            (source / 'plugin.py').write_text('', 'utf-8')
            (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
                'version': '1', 'entry': 'plugin.py', 'services': {'echo': ['invoke']}}), 'utf-8')
            manager = ProcessPackages(Path(root) / 'installed')
            installed = manager.install(source)
            package = Path(installed['package'])
            self.assertFalse((package / '__pycache__').exists())
            (package / '__pycache__').mkdir()
            (package / '__pycache__/plugin.cpython-312.pyc').write_bytes(b'added bytecode')
            with self.assertRaises(PluginError) as failure:
                manager.catalog(package, journal=Path(root) / 'journal')
            self.assertEqual(failure.exception.code, 'PROCESS_PACKAGE_CONTENT_CHANGED')

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

    def test_idle_process_exit_pumps_cleanup_outside_reader(self):
        with tempfile.TemporaryDirectory() as root:
            manager, installed = self.package(root)
            spec = manager.catalog(installed['package'], journal=Path(root) / 'journal')
            disposed = Event()

            def mount(ctx, config):
                ctx.require('echo')
                ctx.defer(disposed.set)

            dependent = PluginSpec('dependent', (), ('echo',), mount)
            with patch.object(ProcessTransport, 'start'):
                host = PluginHost({'version': 1, 'plugins': [
                    {'id': 'process', 'use': spec.key}, {'id': 'dependent', 'use': dependent.key}]},
                    [spec, dependent])
            transport = host.service('echo').transport
            notified = Event()
            on_exit = transport.on_exit

            def notify():
                try:
                    on_exit()
                finally:
                    notified.set()

            transport.on_exit = notify
            transport._process = Mock(stdin=BytesIO(), stdout=BytesIO())
            transport._process.poll.return_value = 19
            transport._reader = Thread(target=transport._read, daemon=True)
            transport._reader.start()
            try:
                self.assertTrue(notified.wait(2), 'Idle process exit must finish lifecycle notification')
                self.assertTrue(disposed.is_set(), 'Idle process exit must finish dependent cleanup')
                transport._reader.join(1)
                self.assertFalse(transport._reader.is_alive())
                self.assertTrue(transport._closed)
                self.assertEqual(host.inspect()['counts']['resources'], 0)
            finally:
                host.close()

    def test_control_write_deadline_is_supervised_without_pending_business_request(self):
        with tempfile.TemporaryDirectory() as root:
            _, installed = self.package(root)
            transport = ProcessTransport(installed['package'], journal=Path(root) / 'journal',
                timeout=1, identity={})
            writing, released, terminated = Event(), Event(), Event()

            def write(payload):
                writing.set()
                released.wait(2)
                raise BrokenPipeError('controlled process stopped')

            def terminate():
                terminated.set()
                released.set()

            process = transport._process = Mock()
            process.poll.side_effect = lambda: 19 if terminated.is_set() else None
            process.terminate.side_effect = terminate
            process.stdin.write.side_effect = write
            request = {'id': 'cancel-control', 'kind': 'cancel'}
            control = _Write(request, b'cancel\n', monotonic() + 0.05, Future(), status='CANCEL_SENT')
            try:
                transport._enqueue(control)
                self.assertTrue(writing.wait(1))
                self.assertEqual(transport._pending, {})
                self.assertTrue(terminated.wait(1), 'Control write must retain independent supervision')
                transport._writer.join(1)
                self.assertFalse(transport._writer.is_alive())
                self.assertTrue(control.future.done())
                self.assertEqual(transport._writes, {})
            finally:
                released.set()
                transport.close()
