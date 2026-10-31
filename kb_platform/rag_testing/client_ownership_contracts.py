import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from kb_platform.platform_plugins import OwnedModelClients, platform_plugins
from kb_platform.plugin_host import PluginHost, PluginSpec


class ClientOwnershipContracts(unittest.TestCase):
    def test_root_cleanup_failure_retains_transferred_delivery_clients_until_consumer_finishes(self):
        closed, blocked = [], [True]
        clients = OwnedModelClients(SimpleNamespace(close=lambda: closed.append('generation')),
                                   SimpleNamespace(close=lambda: closed.append('embedding')))
        def mount(ctx, config):
            ctx.require('model.clients')
            def stop():
                if blocked[0]:
                    raise RuntimeError('consumer still owns model resources')
            ctx.defer(stop)
        with TemporaryDirectory() as root:
            config_path = Path(root) / 'providers.json'
            config_path.write_text(json.dumps({
                'luna': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture', 'api_key': 'synthetic'},
                'google': {'base_url': 'https://fixture.invalid/v1', 'model': 'gemini-embedding-2',
                           'api_key': 'synthetic', 'dimension': 768}}), 'utf-8')
            catalog = (*platform_plugins(SimpleNamespace(storage_root=root), model_clients=clients),
                PluginSpec('consumer', (), ('model.clients',), mount))
            host = PluginHost({'version': 1, 'plugins': [
                {'id': 'clients', 'use': 'platform.model_clients.remote',
                 'config': {'providers_config': str(config_path)}},
                {'id': 'consumer', 'use': 'consumer'}]}, catalog)
            try:
                with self.assertRaises(ExceptionGroup):
                    host.close()
                clients.close_unclaimed()
                self.assertEqual(closed, [])
                self.assertEqual(host.inspect()['state'], 'cleanup_failed')
            finally:
                blocked[0] = False
                host.close()
            clients.close_unclaimed()
            self.assertEqual(closed, ['embedding', 'generation'])

    def test_clients_created_before_failed_composition_close_once_when_unclaimed(self):
        closed = []
        clients = OwnedModelClients(SimpleNamespace(close=lambda: closed.append('generation')),
                                   SimpleNamespace(close=lambda: closed.append('embedding')))
        clients.close_unclaimed()
        clients.close_unclaimed()
        self.assertEqual(closed, ['embedding', 'generation'])
