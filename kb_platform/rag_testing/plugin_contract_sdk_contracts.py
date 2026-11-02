import unittest
from copy import deepcopy
from inspect import CORO_CLOSED, getcoroutinestate
from threading import Event, Thread
from types import SimpleNamespace

from kb_platform.plugin_contracts import ContractApplication, ContractError, ContractRegistry, Method, wire
from kb_platform.plugin_native import PluginError


EMPTY = {'type': 'object', 'additionalProperties': False}
TEXT = {'type': 'string'}


def definition(name, provides, requires):
    return {'implementation': name, 'version': '1', 'executor': 'local',
            'provides': provides, 'requires': requires}


class PluginContractSDKContracts(unittest.TestCase):
    def test_contract_mount_checks_original_signature_before_any_initialization(self):
        async def asynchronous(ctx, config):
            return None

        for mount in (lambda: None, asynchronous):
            with self.subTest(mount=mount):
                application, catalog, args, events = self.setup_application()
                args['mounts'][('provider', '1')] = mount
                with self.assertRaisesRegex(PluginError, 'INVALID_PLUGIN_MOUNT'):
                    ContractApplication(application, catalog, **args)
                self.assertEqual(events, [])

    def test_contract_mount_returned_coroutine_is_closed_and_rolls_back_exports(self):
        application, catalog, args, events = self.setup_application()
        returned = []

        async def pending():
            events.append('not-awaited')

        def provider(ctx, config):
            ctx.defer(lambda: events.append('closed'))
            ctx.provide('greeting', SimpleNamespace(get=lambda request: 'unexpected'))
            result = pending()
            returned.append(result)
            return result

        args['mounts'][('provider', '1')] = provider
        with self.assertRaisesRegex(PluginError, 'INVALID_PLUGIN_MOUNT_RESULT'):
            ContractApplication(application, catalog, **args)
        self.assertEqual(events, ['closed'])
        self.assertEqual(len(returned), 1)
        self.assertEqual(getcoroutinestate(returned[0]), CORO_CLOSED)

    def test_contract_admission_rejects_invalid_methods_and_rolls_back_provider(self):
        async def asynchronous(request):
            return 'unexpected'

        def generator(request):
            yield 'unexpected'

        for callback in (lambda: 'unexpected', asynchronous, generator):
            with self.subTest(callback=callback):
                application, catalog, args, events = self.setup_application()

                def provider(ctx, config):
                    ctx.defer(lambda: events.append('closed'))
                    ctx.provide('greeting', SimpleNamespace(get=callback))

                args['mounts'][('provider', '1')] = provider
                with self.assertRaisesRegex(PluginError, 'CONTRACT_IMPLEMENTATION_INVALID'):
                    ContractApplication(application, catalog, **args)
                self.assertEqual(events, ['closed'])

    def test_sync_contract_closes_accidentally_returned_coroutine_and_releases_lease(self):
        application, catalog, args, events = self.setup_application()
        returned = []

        async def asynchronous():
            return 'unexpected'

        def get(request):
            result = asynchronous()
            returned.append(result)
            return result

        def provider(ctx, config):
            ctx.provide('greeting', SimpleNamespace(get=get))

        args['mounts'][('provider', '1')] = provider
        app = ContractApplication(application, catalog, **args)
        self.addCleanup(app.close)
        with self.assertRaisesRegex(PluginError, 'CONTRACT_IMPLEMENTATION_ASYNC_RESULT'):
            app.run({})
        self.assertEqual(len(returned), 1)
        self.assertEqual(getcoroutinestate(returned[0]), CORO_CLOSED)
        self.assertEqual(app.host.inspect()['leases'], 0)

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

    def test_mount_calls_ready_declared_dependency_with_initialization_lease(self):
        application, catalog, args, events = self.setup_application()

        def mount(ctx, config):
            greeting = ctx.require('source').get({})
            with self.assertRaises(PluginError) as undeclared:
                ctx.require('undeclared')
            self.assertEqual(undeclared.exception.code, 'UNDECLARED_SERVICE_DEPENDENCY')
            with self.assertRaises(PluginError) as external:
                ctx._context.host.call(ctx._bindings['source'], 'get', {})
            self.assertEqual(external.exception.code, 'scope_unavailable')

            class App:
                def run(self, request):
                    return greeting
            ctx.provide('server', App())

        args['mounts'][('app', '1')] = mount
        app = ContractApplication(application, catalog, **args)
        try:
            self.assertEqual(app.run({}), 'hello')
            self.assertEqual(events.count('get'), 1)
            self.assertEqual(app.host.inspect()['leases'], 0)
        finally:
            app.close()

    def test_invalid_configuration_has_no_initialization_effects(self):
        application, catalog, args, events = self.setup_application(config={'unknown': True})
        with self.assertRaisesRegex(PluginError, 'Additional properties'):
            ContractApplication(application, catalog, **args)
        self.assertEqual(events, [])

    def test_invalid_update_and_apply_keep_active_instances_and_service(self):
        application, catalog, args, events = self.setup_application()
        app = ContractApplication(application, catalog, **args)
        self.addCleanup(app.close)
        profile = {'version': 1, 'plugins': [{'id': name, 'use': spec.key, 'config': deepcopy(config)}
                   for name, (spec, config) in app.host._entries.items()]}
        original = next(row['config'] for row in profile['plugins'] if row['id'] == 'backend')
        variants = []
        for field, value in [('configuration', {'unknown': True}), ('configuration', []),
                             ('bindings', {'hidden': 'provider'}), ('command', ['unexpected']),
                             ('declaration', {}), ('unknown', True)]:
            variants.append({**deepcopy(original), field: value})
        variants.append({key: value for key, value in original.items() if key != 'configuration'})
        for operation in ('update', 'apply'):
            for index, config in enumerate(variants):
                with self.subTest(operation=operation, variant=index):
                    events.clear()
                    before = app.host.inspect()
                    with self.assertRaises(PluginError) as caught:
                        if operation == 'update':
                            app.host.update('backend', config=config)
                        else:
                            selected = deepcopy(profile)
                            next(row for row in selected['plugins'] if row['id'] == 'backend')['config'] = config
                            app.host.apply(selected)
                    self.assertEqual(caught.exception.code, 'INVALID_PLUGIN_CONFIG')
                    self.assertEqual(app.host.inspect(), before)
                    self.assertEqual(events, [])
                    self.assertEqual(app.run({}), 'hello')

    def test_valid_configuration_updates_keep_local_schema_references(self):
        application, catalog, args, events = self.setup_application(config={'greeting': 'initial'})
        args['schemas'][('provider', '1')] = {
            '$defs': {'text': TEXT}, 'type': 'object',
            'properties': {'greeting': {'$ref': '#/$defs/text'}},
            'required': ['greeting'], 'additionalProperties': False}

        def provider(ctx, config):
            class Provider:
                def get(self, request):
                    return config['greeting']
            ctx.defer(lambda: events.append('close-provider'))
            ctx.provide('greeting', Provider())

        args['mounts'][('provider', '1')] = provider
        app = ContractApplication(application, catalog, **args)
        self.addCleanup(app.close)
        self.assertEqual(app.run({}), 'initial')
        profile = {'version': 1, 'plugins': [{'id': name, 'use': spec.key, 'config': deepcopy(config)}
                   for name, (spec, config) in app.host._entries.items()]}
        selected = next(row for row in profile['plugins'] if row['id'] == 'backend')
        selected['config']['configuration']['greeting'] = 'updated'
        app.host.update('backend', config=selected['config'])
        self.assertEqual(app.run({}), 'updated')
        selected['config']['configuration']['greeting'] = 'applied'
        app.host.apply(profile)
        self.assertEqual(app.run({}), 'applied')
        self.assertEqual(events.count('close-provider'), 2)

    def test_local_executor_requires_registration_even_with_process_root(self):
        application, catalog, args, events = self.setup_application()
        del args['mounts'][('provider', '1')]
        with self.assertRaises(PluginError) as caught:
            ContractApplication(application, catalog, process_root='.', **args)
        self.assertEqual(caught.exception.code, 'CONTRACT_EXECUTOR_MISSING')
        self.assertEqual(events, [])

    def test_process_declaration_cannot_be_silently_replaced_by_local_mount(self):
        application, catalog, args, events = self.setup_application()
        catalog[1].update(executor='process', command=['provider'])
        with self.assertRaises(PluginError) as caught:
            ContractApplication(application, catalog, process_root='.', **args)
        self.assertEqual(caught.exception.code, 'CONTRACT_EXECUTOR_MISMATCH')
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

    def test_wire_rejects_values_that_json_would_silently_convert(self):
        values = [({'value': ('tuple',)}), {1: 'key'}, float('nan'), float('inf'), object(), '\ud800']
        for value in values:
            with self.subTest(value=repr(value)), self.assertRaises(PluginError) as caught:
                wire(value)
            self.assertEqual(caught.exception.code, 'CONTRACT_VALUE_NOT_SERIALIZABLE')
        source = {'nested': [{'value': True}, None, 3.5]}
        copy = wire(source)
        copy['nested'][0]['value'] = False
        self.assertTrue(source['nested'][0]['value'])

    def test_schema_and_registry_declarations_are_snapshots(self):
        request = {'type': 'object', 'properties': {'name': {'type': 'string'}},
                   'required': ['name'], 'additionalProperties': False}
        method = Method(request, TEXT)
        methods = {'get': method}
        registry = ContractRegistry({('example', 1): methods})
        request['properties']['name']['type'] = 'integer'
        method.request['properties']['name']['type'] = 'integer'
        methods.clear()
        selected = registry.methods({'id': 'example', 'major': 1})['get']
        self.assertEqual(selected.validate({'name': 'stable'}), {'name': 'stable'})
        with self.assertRaises(PluginError):
            selected.validate({'name': 5})
        with self.assertRaises(TypeError):
            registry.contracts[('example', 2)] = {}

    def test_external_schema_references_are_rejected_before_mount(self):
        for key, value in [('$ref', 'https://example.com/schema'), ('$ref', 'file:///tmp/schema'),
                           ('$id', 'https://example.com/base'), ('$dynamicRef', 'remote.json')]:
            with self.subTest(key=key, value=value), self.assertRaises(PluginError) as caught:
                Method({'type': 'object', 'properties': {'name': {key: value}}}, TEXT)
            self.assertEqual(caught.exception.code, 'CONTRACT_EXTERNAL_SCHEMA_REFERENCE')
        method = Method({'$defs': {'name': TEXT}, '$ref': '#/$defs/name'}, TEXT)
        self.assertEqual(method.validate('local'), 'local')

    def test_public_export_call_and_structured_business_error(self):
        application, catalog, args, _ = self.setup_application()

        class Rejected(Exception):
            code = 'ACCESS_DENIED'
            status_code = 403
            details = {'resource': 'document'}
            call_record = {'operation': 'query', 'status': 'ERROR'}

        def provider(ctx, config):
            class Provider:
                def get(self, request):
                    raise Rejected('Document is inaccessible')
            ctx.provide('greeting', Provider())

        args['mounts'][('provider', '1')] = provider
        app = ContractApplication(application, catalog, **args)
        try:
            with self.assertRaises(ContractError) as caught:
                app.call('backend', 'greeting', 'get', {})
            self.assertEqual(caught.exception.to_dict(), {'code': 'ACCESS_DENIED',
                'message': 'Document is inaccessible', 'status_code': 403,
                'details': {'resource': 'document', 'call_record': Rejected.call_record}})
            with self.assertRaises(PluginError) as unknown:
                app.call('backend', 'missing', 'get', {})
            self.assertEqual(unknown.exception.code, 'CONTRACT_EXPORT_UNKNOWN')
            with self.assertRaises(PluginError) as private:
                app.call('backend', 'greeting', '__getattribute__', '_implementation')
            self.assertEqual(private.exception.code, 'CONTRACT_METHOD_UNKNOWN')
        finally:
            app.close()

    def test_stop_cancels_active_calls_before_cleanup(self):
        application, catalog, args, events = self.setup_application()
        entered = Event()

        def provider(ctx, config):
            class Provider:
                def get(self, request):
                    entered.set()
                    if not ctx.wait_cancelled(5):
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
