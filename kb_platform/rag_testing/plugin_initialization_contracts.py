import unittest

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


class PluginInitializationContracts(unittest.TestCase):
    def test_initializing_plugin_can_lease_its_own_resource_without_publishing_its_service(self):
        contexts = []

        def mount(ctx, config):
            contexts.append(ctx)
            with ctx.lease():
                self.assertGreater(ctx.host.inspect()['leases'], 0)
                with self.assertRaises(PluginError):
                    ctx.host.service('ready')
            ctx.provide('ready', 'complete')

        with PluginHost({'version': 1, 'plugins': [{'id': 'consumer', 'use': 'consumer'}]},
                        (PluginSpec('consumer', ('ready',), (), mount),)) as host:
            self.assertEqual(host.service('ready'), 'complete')
            self.assertEqual(host.inspect()['leases'], 0)
            with contexts[0].lease():
                self.assertGreater(host.inspect()['leases'], 0)
        self.assertEqual(host.inspect()['state'], 'disposed')
        with self.assertRaises(PluginError):
            with contexts[0].lease():
                self.fail('disposed initialization context acquired a lease')

    def test_failed_mount_releases_initialization_lease_before_resource_cleanup(self):
        contexts, cleaned = [], []

        def mount(ctx, config):
            contexts.append(ctx)
            ctx.defer(lambda: cleaned.append(ctx.host.inspect()['leases']))
            with ctx.lease():
                self.assertGreater(ctx.host.inspect()['leases'], 0)
                raise ValueError('synthetic failure after initialization write')

        with self.assertRaisesRegex(ValueError, 'synthetic failure after initialization write'):
            PluginHost({'version': 1, 'plugins': [{'id': 'consumer', 'use': 'consumer'}]},
                       (PluginSpec('consumer', ('ready',), (), mount),))
        self.assertEqual(cleaned, [0])
        self.assertEqual(contexts[0].host.inspect()['state'], 'disposed')
        self.assertEqual(contexts[0].host.inspect()['leases'], 0)
        with self.assertRaises(PluginError):
            with contexts[0].lease():
                self.fail('failed initialization context acquired a lease')
