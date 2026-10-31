import unittest

from kb_platform.model_plugins import model_plugins
from kb_platform.model_service import Capability, ModelInfo, ModelRequest, ModelResult, ModelService
from kb_platform.plugin_host import PluginHost
from kb_platform.plugin_native import PluginError


class BorrowedModelContracts(unittest.TestCase):
    def test_borrowed_registry_preserves_external_bindings_across_two_host_lifetimes(self):
        calls = []

        class Adapter:
            info = ModelInfo('synthetic', 'local', frozenset({Capability.CHAT}))

            def invoke(self, request):
                calls.append(request)
                return ModelResult('synthetic', 'local', Capability.CHAT, {'text': 'local answer'})

            def close(self):
                raise AssertionError('borrowed adapter must not be closed by a host')

        models = ModelService()
        models.register('generation', Adapter(), owned=True)
        bound = models.bind('generation', Capability.CHAT)
        before = models.inspect()
        request = ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': 'hello'}]})
        profile = {'version': 1, 'plugins': [{'id': 'models', 'use': 'models.service'}]}
        first = PluginHost(profile, model_plugins(models))
        second = None
        try:
            second = PluginHost(profile, model_plugins(models))
            self.assertEqual(bound.invoke(request).data, {'text': 'local answer'})
            self.assertEqual(first.call('models', 'invoke', 'generation', request).data,
                             {'text': 'local answer'})
            self.assertEqual(models.inspect(), before)
            first.close()
            self.assertEqual(bound.invoke(request).data, {'text': 'local answer'})
            self.assertEqual(second.call('models', 'invoke', 'generation', request).data,
                             {'text': 'local answer'})
            with self.assertRaises(PluginError):
                first.call('models', 'invoke', 'generation', request)
            second.close()
            self.assertEqual(bound.invoke(request).data, {'text': 'local answer'})
            self.assertEqual(models.inspect(), before)
            self.assertEqual(len(calls), 5)
        finally:
            first.close()
            if second is not None:
                second.close()
