import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError
from kb_platform.plugin_process import PROTOCOL, ProcessPackages, ProcessTransport


class PluginBridgeReviewContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'plugin.py').write_text('', encoding='utf-8')
        (self.source / 'plugin.json').write_text(json.dumps({'protocol': PROTOCOL, 'name': 'review',
            'version': '1', 'entry': 'plugin.py', 'services': {'review.echo': ['echo']}}), encoding='utf-8')

    def transport(self):
        return ProcessTransport(self.source, journal=self.root / 'journal', timeout=1,
            identity={'runtime': 1, 'instance': 2, 'generation': 1, 'operation': 3})

    def test_journal_failure_before_send_does_not_leak_a_pending_request(self):
        transport = self.transport()
        transport._process = SimpleNamespace(poll=lambda: None)
        with patch.object(transport, '_record', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                transport.request('invoke', service='review.echo', method='echo')
        self.assertEqual(transport._pending, {})

    def test_close_rejects_a_new_request_before_process_termination_finishes(self):
        transport, sent = self.transport(), []

        class Input(io.BytesIO):
            def write(inner, payload):
                request = json.loads(payload)
                sent.append(request['kind'])
                transport._pending[request['id']].set_result({'protocol': PROTOCOL,
                    'id': request['id'], 'ok': True, 'result': None})
                return len(payload)

        transport._process = SimpleNamespace(poll=lambda: None, stdin=Input(), stdout=io.BytesIO())
        closing, release, errors = Event(), Event(), []

        def terminate():
            closing.set()
            if not release.wait(3):
                raise RuntimeError('close barrier was not released')

        def close():
            try:
                transport.close()
            except BaseException as error:
                errors.append(error)

        with patch.object(transport, '_terminate', terminate):
            worker = Thread(target=close, daemon=True)
            worker.start()
            try:
                self.assertTrue(closing.wait(3))
                with self.assertRaises(PluginError):
                    transport.request('invoke', service='review.echo', method='echo')
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sent, ['shutdown'])

    def test_borrowed_process_service_still_uses_rust_admission_after_drain(self):
        manager = ProcessPackages(self.root / 'installed')
        package = manager.install(self.source)
        spec = manager.catalog(package['package'], journal=self.root / 'journal')
        profile = {'version': 1, 'plugins': [{'id': 'process', 'use': spec.key}]}
        with patch.object(ProcessTransport, 'start'), patch.object(ProcessTransport, 'close'), \
                patch.object(ProcessTransport, 'request', return_value='unexpected send') as request:
            with PluginHost(profile, [spec]) as host:
                service = host.service('review.echo')
                host.drain()
                with self.assertRaises(PluginError):
                    service.echo('rejected')
                request.assert_not_called()

    def test_old_context_owner_lease_cannot_admit_against_a_new_generation(self):
        effects = []

        def mount(context, config):
            class Service:
                def work(self):
                    with context.lease():
                        effects.append(dict(context.identity))

            context.provide('review.owned', Service())

        profile = {'version': 1, 'plugins': [{'id': 'owned', 'use': 'owned'}]}
        with PluginHost(profile, [PluginSpec('owned', ('review.owned',), (), mount)]) as host:
            old = host.service('review.owned')
            host.update('owned', enabled=False)
            host.update('owned', enabled=True)
            with self.assertRaises(PluginError):
                old.work()
            self.assertEqual(effects, [])
            host.service('review.owned').work()
            self.assertEqual(len(effects), 1)
