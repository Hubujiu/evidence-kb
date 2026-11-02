import unittest
from threading import Event, Thread

from kb_platform.plugin_contracts import ContractApplication, ContractRegistry, Method
from kb_platform.plugin_native import PluginError


EMPTY = {'type': 'object', 'additionalProperties': False}
TEXT = {'type': 'string'}


def definition(name, provides, requires):
    return {'implementation': name, 'version': '1', 'command': ['python', name],
            'provides': provides, 'requires': requires}


class PluginContractSDKContracts(unittest.TestCase):
    def setup_application(self, *, response='hello', config=None, replacement='provider'):
        events = []
        registry = ContractRegistry({('application.run', 1): {'run': Method(EMPTY, TEXT)},
                                     ('greeting', 1): {'get': Method(EMPTY, TEXT)}})
        greeting = {'id': 'greeting', 'major': 1}
        catalog = [definition('app', {'server': {'id': 'application.run', 'major': 1}},
                              {'source': {'contract': greeting}}),
                   definition(replacement, {'greeting': greeting}, {})]
        application = {'version': 1, 'plugins': {
            'api': {'implementation': 'app', 'version': '1', 'config': {}},
            'backend': {'implementation': replacement, 'version': '1', 'config': config or {}}},
            'bindings': {'api': {'source': {'plugin': 'backend', 'export': 'greeting'}}},
            'entrypoint': {'plugin': 'api', 'export': 'server'}}

        class Provider:
            def get(self, request):
                events.append('get')
                return response

        def provider(ctx, config):
            events.append('provider')
            ctx.defer(lambda: events.append('close-provider'))
            ctx.provide('greeting', Provider())

        def mount(ctx, config):
            events.append('app')
            dependency = ctx.require('source')
            ctx.defer(lambda: events.append('close-app'))

            class App:
                def run(self, request):
                    return dependency.get(request)
            ctx.provide('server', App())

        args = {'registry': registry, 'mounts': {('app', '1'): mount, (replacement, '1'): provider},
                'schemas': {('app', '1'): EMPTY, (replacement, '1'): EMPTY}}
        return application, catalog, args, events

    def test_compiled_aliases_execute_with_lifecycle_and_cleanup(self):
        application, catalog, args, events = self.setup_application()
        app = ContractApplication(application, catalog, **args)
        try:
            self.assertEqual(app.run({}), 'hello')
            self.assertEqual(events, ['provider', 'app', 'get'])
        finally:
            app.close()
        self.assertEqual(events[-2:], ['close-app', 'close-provider'])

    def test_replacement_keeps_consumer_binding_and_interface(self):
        application, catalog, args, _ = self.setup_application(replacement='another', response='replacement')
        app = ContractApplication(application, catalog, **args)
        try:
            self.assertEqual(app.run({}), 'replacement')
        finally:
            app.close()

    def test_invalid_configuration_has_no_initialization_effects(self):
        application, catalog, args, events = self.setup_application(config={'unknown': True})
        with self.assertRaisesRegex(PluginError, 'Additional properties'):
            ContractApplication(application, catalog, **args)
        self.assertEqual(events, [])

    def test_invalid_request_does_not_reach_provider(self):
        application, catalog, args, events = self.setup_application()
        app = ContractApplication(application, catalog, **args)
        try:
            with self.assertRaises(PluginError) as caught:
                app.run({'unknown': True})
            self.assertEqual(caught.exception.code, 'CONTRACT_REQUEST_INVALID')
            self.assertNotIn('get', events)
        finally:
            app.close()

    def test_invalid_response_is_rejected_at_provider_boundary(self):
        application, catalog, args, _ = self.setup_application(response={'private': 'object'})
        app = ContractApplication(application, catalog, **args)
        try:
            with self.assertRaises(PluginError) as caught:
                app.run({})
            self.assertEqual(caught.exception.code, 'CONTRACT_RESPONSE_INVALID')
        finally:
            app.close()

    def test_stop_cancels_active_calls_before_cleanup(self):
        application, catalog, args, events = self.setup_application()
        entered = Event()

        def provider(ctx, config):
            class Provider:
                def get(self, request):
                    signal = ctx._context.host.current_call['cancel_signal']
                    entered.set()
                    if not signal.wait(5):
                        raise RuntimeError('CANCELLATION_NOT_DELIVERED')
                    events.append('finished')
                    return 'stopped'
            ctx.defer(lambda: events.append('closed'))
            ctx.provide('greeting', Provider())

        args['mounts'][('provider', '1')] = provider
        app = ContractApplication(application, catalog, **args)
        results = []

        def run():
            try:
                results.append(app.run({}))
            except BaseException as error:
                results.append(error)

        thread = Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            app.stop()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, ['stopped'])
        finally:
            thread.join(6)
            app.close()
        self.assertIn('closed', events)
        self.assertLess(events.index('finished'), events.index('closed'))
