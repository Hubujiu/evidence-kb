from concurrent.futures import Future
from io import BytesIO
import json
from pathlib import Path
from queue import Queue
import sys
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

    def test_dependency_manifest_rejects_invalid_or_duplicate_service_names(self):
        for requires in ('shared.model', [None], [''], ['shared.model', 'shared.model']):
            with self.subTest(requires=requires), tempfile.TemporaryDirectory() as root:
                source = Path(root) / 'source'
                source.mkdir()
                (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
                    'version': '1', 'command': ['worker'], 'services': {'echo': ['invoke']},
                    'requires': requires}), 'utf-8')
                manager = ProcessPackages(Path(root) / 'installed')
                with self.assertRaises(PluginError) as failure:
                    manager.install(source)
                self.assertEqual(failure.exception.code, 'INVALID_PROCESS_DEPENDENCY')

    def test_callbacks_queue_without_execution_and_reject_duplicate_unknown_or_completed_parent(self):
        with tempfile.TemporaryDirectory() as root:
            _, installed = self.package(root)
            transport = ProcessTransport(installed['package'], journal=Path(root) / 'journal', timeout=1, identity={})
            transport.invoke_dependency = Mock()
            transport._pending['parent'] = Future()
            callbacks = Queue()
            transport._callbacks['parent'] = callbacks, set()
            frame = {'protocol': PROTOCOL, 'id': 'call', 'kind': 'call', 'parent': 'parent',
                'service': 'shared.model', 'method': 'generate', 'args': ['hello'], 'kwargs': {}}
            transport._queue_callback(frame)
            self.assertEqual(callbacks.get_nowait(), frame)
            transport.invoke_dependency.assert_not_called()
            for value, code in ((frame, 'PROCESS_CALLBACK_DUPLICATE_OR_LIMIT'),
                    ({**frame, 'id': 'other', 'parent': 'missing'}, 'PROCESS_CALLBACK_PARENT_UNAVAILABLE')):
                with self.assertRaises(PluginError) as failure:
                    transport._queue_callback(value)
                self.assertEqual(failure.exception.code, code)
            transport._pending['parent'].set_result({})
            with self.assertRaises(PluginError) as failure:
                transport._queue_callback({**frame, 'id': 'late'})
            self.assertEqual(failure.exception.code, 'PROCESS_CALLBACK_PARENT_UNAVAILABLE')
            transport.invoke_dependency.assert_not_called()

    def test_undeclared_callback_returns_error_without_invoking_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            _, installed = self.package(root)
            transport = ProcessTransport(installed['package'], journal=Path(root) / 'journal', timeout=1, identity={})
            transport.invoke_dependency = Mock()
            sent = []

            def send(write):
                sent.append(json.loads(write.payload))
                write.future.set_result(None)

            call = {'id': 'call', 'parent': 'parent', 'service': 'undeclared', 'method': 'generate',
                'args': [], 'kwargs': {}}
            with patch.object(transport, '_enqueue', send):
                transport._invoke_callback(call, monotonic() + 1, None)
            transport.invoke_dependency.assert_not_called()
            self.assertFalse(sent[0]['ok'])
            self.assertEqual(sent[0]['error']['code'], 'UNDECLARED_SERVICE_DEPENDENCY')

    def test_failed_callback_response_send_retains_unknown_without_reinvoking_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            _, installed = self.package(root)
            journal = Path(root) / 'journal'
            transport = ProcessTransport(installed['package'], journal=journal, timeout=1, identity={})
            transport.manifest['requires'] = ['shared.model']
            transport.invoke_dependency = Mock(return_value={'answer': 'completed'})

            def fail(write):
                write.sent = True
                transport._record(write.request, write.status)
                write.future.set_exception(BrokenPipeError('Response transport closed'))

            call = {'id': 'call', 'parent': 'parent', 'service': 'shared.model', 'method': 'generate',
                'args': [], 'kwargs': {}}
            with patch.object(transport, '_enqueue', fail):
                with self.assertRaises(BrokenPipeError):
                    transport._invoke_callback(call, monotonic() + 1, None)
            transport.invoke_dependency.assert_called_once()
            rows = [json.loads(path.read_text('utf-8')) for path in journal.glob('*.json')]
            self.assertEqual(sorted(row['status'] for row in rows), ['PREPARED', 'SENT', 'UNKNOWN'])
            self.assertEqual(len({row['request']['id'] for row in rows}), 1)

    def test_cancelled_callback_does_not_invoke_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            _, installed = self.package(root)
            transport = ProcessTransport(installed['package'], journal=Path(root) / 'journal', timeout=1, identity={})
            transport.manifest['requires'] = ['shared.model']
            transport.invoke_dependency = Mock()
            cancelled, sent = Event(), []
            cancelled.set()

            def send(write):
                sent.append(json.loads(write.payload))
                write.future.set_result(None)

            call = {'id': 'call', 'parent': 'parent', 'service': 'shared.model', 'method': 'generate',
                'args': [], 'kwargs': {}}
            with patch.object(transport, '_enqueue', send):
                transport._invoke_callback(call, monotonic() + 1, cancelled)
            transport.invoke_dependency.assert_not_called()
            self.assertEqual(sent[0]['error']['code'], 'PROCESS_CALLBACK_CANCELLED')

    def test_command_launch_passes_literal_argv_without_inserting_python_options(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source'
            source.mkdir()
            command = ['worker', '', 'two words', 'literal&value', '$unchanged']
            (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
                'version': '1', 'command': command, 'services': {'echo': ['invoke']}}), 'utf-8')
            transport = ProcessTransport(source, journal=Path(root) / 'journal', timeout=1, identity={})
            response = {'protocol': PROTOCOL, 'services': {'echo': ['invoke']},
                'runtime': {'language': 'rust', 'version': '1', 'executable': 'worker'}}
            process = Mock(stdout=BytesIO(), stdin=BytesIO())
            with patch('kb_platform.plugin_process.subprocess.Popen', return_value=process) as popen, \
                    patch('kb_platform.plugin_process.Thread'), patch.object(transport, 'request', return_value=response):
                transport.start()
            self.assertEqual(popen.call_args.args, (command,))
            self.assertEqual(popen.call_args.kwargs['cwd'], source.resolve())
            self.assertNotIn('shell', popen.call_args.kwargs)
            self.assertEqual(transport.runtime_info['language'], 'rust')

    def test_command_manifest_rejects_ambiguous_or_invalid_launch_before_install(self):
        cases = [
            {'command': []}, {'command': 'python plugin.py'}, {'command': [4]},
            {'command': ['']}, {'command': ['  ']}, {'command': ['worker', None]},
            {'command': ['worker', 'bad\0argument']},
            {'command': ['worker'], 'entry': 'plugin.py'},
            {'command': ['worker'], 'python': sys.executable},
        ]
        for launch in cases:
            with self.subTest(launch=launch), tempfile.TemporaryDirectory() as root:
                source = Path(root) / 'source'
                source.mkdir()
                (source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'fixture',
                    'version': '1', 'services': {'echo': ['invoke']}, **launch}), 'utf-8')
                manager = ProcessPackages(Path(root) / 'installed')
                with self.assertRaises(PluginError) as failure:
                    manager.install(source)
                self.assertEqual(failure.exception.code, 'INVALID_PROCESS_COMMAND')
                self.assertEqual(list(manager.root.iterdir()), [])

    def test_handshake_rejects_missing_or_empty_language_neutral_runtime_fields(self):
        for runtime in ({'python_version': '3.12', 'executable': 'python'},
                {'language': 'rust', 'version': '', 'executable': 'worker'},
                {'language': 7, 'version': '1', 'executable': 'worker'},
                {'language': 'rust', 'version': '1', 'executable': '   '}):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as root:
                _, installed = self.package(root)
                transport = ProcessTransport(installed['package'], journal=Path(root) / 'journal',
                    timeout=1, identity={})
                response = {'protocol': PROTOCOL, 'services': {'echo': ['invoke']}, 'runtime': runtime}
                process = Mock(stdout=BytesIO(), stdin=BytesIO())
                with patch('kb_platform.plugin_process.subprocess.Popen', return_value=process), \
                        patch('kb_platform.plugin_process.Thread'), patch.object(transport, 'request', return_value=response):
                    with self.assertRaises(PluginError) as failure:
                        transport.start()
                    self.assertEqual(failure.exception.code, 'PROCESS_HANDSHAKE_MISMATCH')

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

    def test_interrupted_metadata_is_never_published_and_retry_commits_complete_record(self):
        with tempfile.TemporaryDirectory() as root:
            manager, existing = self.package(root)
            source = Path(root) / 'source'
            manifest = json.loads((source / 'plugin.json').read_text('utf-8'))
            manifest['name'] = 'new-package'
            (source / 'plugin.json').write_text(json.dumps(manifest), 'utf-8')
            entered, release = Event(), Event()
            failures = []
            dump = json.dump

            def interrupted(value, stream, **kwargs):
                if isinstance(value, dict) and 'contents' in value:
                    stream.write('{"manifest":')
                    stream.flush()
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError('Installation write barrier expired')
                    raise OSError('Interrupted installation metadata write')
                return dump(value, stream, **kwargs)

            def install():
                try:
                    manager.install(source)
                except BaseException as error:
                    failures.append(error)

            with patch('kb_platform.plugin_process.json.dump', interrupted):
                worker = Thread(target=install, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(len(manager.installed()), 1)
                    self.assertEqual(len(manager.catalogs(journal=Path(root) / 'journal')), 1)
                    self.assertEqual(manager.catalog(existing['package'],
                        journal=Path(root) / 'journal').key, 'process.fixture')
                    self.assertFalse((manager.root / 'new-package.active.json').exists())
                finally:
                    release.set()
                    worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual([type(error) for error in failures], [OSError])
            self.assertEqual(len(manager.installed()), 1)
            self.assertEqual(list(manager.root.glob('*.tmp')), [])
            retried = manager.install(source)
            self.assertEqual(len(manager.catalogs(journal=Path(root) / 'journal')), 2)
            self.assertEqual(manager.catalog(retried['package'],
                journal=Path(root) / 'journal').key, 'process.new-package')

    def test_retry_rejects_corrupt_existing_record_before_switching_active_pointer(self):
        with tempfile.TemporaryDirectory() as root:
            manager, existing = self.package(root)
            source = Path(root) / 'source'
            (source / 'helper.py').write_text('value = "new version"\n', 'utf-8')
            replacement = manager.install(source)
            active = manager.root / 'fixture.active.json'
            active.write_text(json.dumps({'digest': existing['digest']}), 'utf-8')
            metadata = manager.root / f'{replacement["digest"]}.installation.json'
            metadata.write_text('{"manifest":', 'utf-8')
            self.assertEqual(len(manager.catalogs(journal=Path(root) / 'journal')), 1)
            with self.assertRaises(PluginError) as failure:
                manager.install(source)
            self.assertEqual(failure.exception.code, 'INVALID_PROCESS_INSTALLATION')
            self.assertEqual(json.loads(active.read_text('utf-8')), {'digest': existing['digest']})
            self.assertEqual(manager.catalog(existing['package'],
                journal=Path(root) / 'journal').key, 'process.fixture')

    def test_exit_notification_only_disables_its_current_generation(self):
        with tempfile.TemporaryDirectory() as root:
            manager, installed = self.package(root)
            spec = manager.catalog(installed['package'], journal=Path(root) / 'journal')
            with patch.object(ProcessTransport, 'start'):
                host = PluginHost({'version': 1, 'plugins': [
                    {'id': 'process', 'use': spec.key}]}, [spec])
                try:
                    old = host.service('echo').transport
                    host.update('process', config={'timeout': 3})
                    current = host.service('echo').transport
                    self.assertNotEqual(old.identity['generation'], current.identity['generation'])
                    self.assertEqual(old.on_exit(), {'accepted': False})
                    self.assertIs(host.service('echo').transport, current)
                    self.assertFalse(current._closed)
                    self.assertEqual(current.on_exit(), {'accepted': True})
                    self.assertTrue(current._closed)
                    self.assertEqual(host.inspect()['counts']['resources'], 0)
                finally:
                    host.close()
                self.assertEqual(current.on_exit(), {'accepted': False})

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
