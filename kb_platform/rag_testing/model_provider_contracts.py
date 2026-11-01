from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

import httpx

from kb_platform.model_plugins import model_plugins
from kb_platform.model_configuration import configured_adapter
from kb_platform.model_provider_plugins import compile_model_plugins, model_route_marker
from kb_platform.model_service import Capability, ModelRequest, ModelService
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


_HttpClient = httpx.Client


class ModelProviderContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sent, self.entered, self.release = [], Event(), Event()
        self.block, self.disconnect = False, False
        self.addCleanup(self.release.set)
        self.endpoint, self.transport = self.endpoint_transport()
        self.clients = []
        clients = patch('kb_platform.model_provider_plugins.httpx.Client', side_effect=self.create_client)
        clients.start()
        self.addCleanup(clients.stop)
        self.path = self.root / 'models.json'
        self.configuration = {'version': 1,
            'budget': {'path': str(self.root / 'budget.sqlite3'), 'caps': {'requests': 10}},
            'providers': {name: {'adapter': 'openai-compatible', 'base_url': self.endpoint,
                                'api_key_env': 'MODEL_LOCAL_' + name.upper()} for name in ('a', 'b')},
            'models': {'generation': {'provider': 'a', 'model': 'chat-a', 'capabilities': ['chat'],
                                      'reservation': {'requests': 1}},
                       'embedding': {'provider': 'a', 'model': 'embed-a', 'capabilities': ['embedding'],
                                     'dimension': 2, 'reservation': {'requests': 1}},
                       'alternative': {'provider': 'b', 'model': 'chat-b', 'capabilities': ['chat'],
                                       'reservation': {'requests': 1}}}}
        self.write_configuration()
        environment = patch.dict('os.environ', {'MODEL_LOCAL_A': 'private-a', 'MODEL_LOCAL_B': 'private-b'})
        environment.start()
        self.addCleanup(environment.stop)

    def endpoint_transport(self):
        def handle(request):
            payload = self.response_body(request.url.path, json.loads(request.content),
                                         request.headers.get('Authorization'))
            if payload is None:
                raise httpx.ReadError('fixture response lost after request', request=request)
            return httpx.Response(200, json=payload)

        return 'https://fixture.invalid/v1', httpx.MockTransport(handle)

    def response_body(self, path, body, authorization):
        self.sent.append((path, body, authorization))
        self.entered.set()
        if self.block:
            self.release.wait(10)
        if self.disconnect:
            return None
        if path.endswith('/embeddings'):
            return {'data': [{'index': index, 'embedding': [1., 2.]}
                             for index, _ in enumerate(body['input'])]}
        return {'choices': [{'message': {'content': body['model']}, 'finish_reason': 'stop'}]}

    def create_client(self, **options):
        if self.transport is not None:
            options['transport'] = self.transport
        client = _HttpClient(**options)
        client.close = Mock(wraps=client.close)
        self.clients.append(client)
        return client

    def write_configuration(self):
        self.path.write_text(json.dumps(self.configuration), 'utf-8')

    def compiled(self, *, views=False):
        profile = {'version': 1, 'plugins': [{'id': 'provider', 'use': 'models.service',
                   'config': {'config_path': str(self.path)}}]}
        if views:
            profile['plugins'].append({'id': 'embedding', 'use': 'models.embedding', 'config': {'dimension': 2}})
        return compile_model_plugins(profile, model_plugins())

    @staticmethod
    def request():
        return ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': 'local fixture'}]})

    def provider_id(self, profile, route):
        marker = 'models.route.' + route
        catalog = dict((spec.key, spec) for spec in self.catalog)
        return next(row['id'] for row in profile['plugins'] if marker in catalog[row['use']].provides)

    def test_routes_share_budget_and_provider_ownership_without_hidden_construction(self):
        profile, self.catalog = self.compiled(views=True)
        self.assertEqual(next(row for row in profile['plugins'] if row['id'] == 'provider')['use'], 'models.budget')
        with patch('kb_platform.model_configuration.from_config', side_effect=AssertionError('hidden constructor')):
            with PluginHost(profile, self.catalog) as host:
                models, ledger = host.service('models'), host.service('models.budget')
                routes = models.inspect()
                self.assertEqual(routes['generation']['owner_identity'], routes['embedding']['owner_identity'])
                self.assertNotEqual(routes['generation']['owner_identity'], routes['alternative']['owner_identity'])
                self.assertEqual(models.invoke('generation', self.request()).data['text'], 'chat-a')
                self.assertEqual(models.invoke('alternative', self.request()).data['text'], 'chat-b')
                self.assertEqual(host.call('embedding', 'query', 'local text'), [1., 2.])
                self.assertEqual(ledger.summary()['physical_requests'], 3)
                self.assertEqual(ledger.summary()['effective_usage_or_reserved'], {'requests': 3})
                state = json.dumps(host.inspect())
                self.assertNotIn('private-a', state)
                self.assertNotIn('private-b', state)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(models.catalog(), {})

    def test_referenced_provider_change_reloads_only_its_instance_and_ready_consumers(self):
        profile, self.catalog = self.compiled(views=True)
        with PluginHost(profile, self.catalog) as host:
            models, ledger = host.service('models'), host.service('models.budget')
            old, other = models.bind('generation', Capability.CHAT), models.bind('alternative', Capability.CHAT)
            view = host.bind('embedding')
            self.configuration['models']['generation']['model'] = 'chat-a-new'
            self.write_configuration()
            host.apply(profile)
            self.assertIs(host.service('models'), models)
            self.assertIs(host.service('models.budget'), ledger)
            self.assertEqual(other.registration_id, models.bind('alternative', Capability.CHAT).registration_id)
            self.assertNotEqual(old.registration_id, models.bind('generation', Capability.CHAT).registration_id)
            with self.assertRaisesRegex(RuntimeError, 'MODEL_BINDING_CHANGED'):
                old.invoke(self.request())
            with self.assertRaises(PluginError):
                view.call('query', 'stale view')
            self.assertEqual(host.call('embedding', 'query', 'new view'), [1., 2.])
            self.assertEqual(models.invoke('generation', self.request()).data['text'], 'chat-a-new')

    def test_invalid_changed_capability_preserves_live_provider_and_existing_budget(self):
        profile, self.catalog = self.compiled()
        with PluginHost(profile, self.catalog) as host:
            models, ledger = host.service('models'), host.service('models.budget')
            models.invoke('generation', self.request())
            before, routes = host.inspect()['instances'], models.inspect()
            self.configuration['models']['generation']['capabilities'] = ['embedding']
            self.write_configuration()
            with self.assertRaisesRegex(ValueError, 'MODEL_ROUTE_CAPABILITY_MISMATCH'):
                host.apply(profile)
            self.assertEqual(host.inspect()['instances'], before)
            self.assertEqual(models.inspect(), routes)
            self.assertEqual(ledger.summary()['physical_requests'], 1)
            self.assertEqual(len(self.sent), 1)

    def test_provider_disable_keeps_independent_route_and_registry(self):
        profile, self.catalog = self.compiled()
        with PluginHost(profile, self.catalog) as host:
            models = host.service('models')
            old, other = models.bind('generation', Capability.CHAT), models.bind('alternative', Capability.CHAT)
            provider = self.provider_id(profile, 'generation')
            host.update(provider, enabled=False)
            self.assertIs(host.service('models'), models)
            self.assertEqual(set(models.catalog()), {'alternative'})
            with self.assertRaisesRegex(RuntimeError, 'MODEL_ROUTE_UNAVAILABLE'):
                old.invoke(self.request())
            self.assertEqual(other.invoke(self.request()).data['text'], 'chat-b')
            host.update(provider, enabled=True)
            self.assertEqual(models.invoke('generation', self.request()).data['text'], 'chat-a')

    def test_host_drain_keeps_endpoint_open_until_call_finishes_then_closes_once(self):
        profile, self.catalog = self.compiled()
        host = PluginHost(profile, self.catalog)
        self.addCleanup(host.close)
        models, result, failures = host.service('models'), [], []
        self.block = True

        def invoke():
            try:
                result.append(models.invoke('generation', self.request()).data)
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=invoke, daemon=True)
        thread.start()
        self.assertTrue(self.entered.wait(3))
        host.drain()
        self.assertFalse(self.clients[0].is_closed)
        self.release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result, [{'text': 'chat-a', 'finish_reason': 'stop'}])
        host.close()
        host.close()
        for client in self.clients:
            client.close.assert_called_once_with()
            self.assertTrue(client.is_closed)

    def test_unknown_physical_result_survives_host_restart_and_blocks_resend(self):
        profile, self.catalog = self.compiled()
        self.disconnect = True
        with PluginHost(profile, self.catalog) as host:
            with self.assertRaisesRegex(RuntimeError, 'PROVIDER_OUTCOME_UNKNOWN'):
                host.service('models').invoke('generation', self.request())
            self.assertEqual(host.service('models.budget').summary()['pending_or_unknown'], 1)
        self.disconnect = False
        with PluginHost(profile, self.catalog) as host:
            with self.assertRaisesRegex(ValueError, 'UNRESOLVED_PHYSICAL_REQUEST'):
                host.service('models').invoke('alternative', self.request())
            self.assertEqual(host.service('models.budget').summary()['pending_or_unknown'], 1)
            self.assertEqual(host.service('models.budget').summary()['effective_usage_or_reserved'], {'requests': 1})
        self.assertEqual(len(self.sent), 1)

    def test_graph_shape_change_requires_explicit_recompile_without_mutating_existing_instances(self):
        profile, self.catalog = self.compiled()
        with PluginHost(profile, self.catalog) as host:
            before = host.inspect()['instances']
            self.configuration['models']['extra'] = deepcopy(self.configuration['models']['generation'])
            self.write_configuration()
            with self.assertRaisesRegex(ValueError, 'MODEL_PROVIDER_GRAPH_RECOMPILE_REQUIRED'):
                host.apply(profile)
            self.assertEqual(host.inspect()['instances'], before)
            self.assertNotIn('extra', host.service('models').catalog())

    def test_configured_registry_cannot_construct_providers_without_compilation(self):
        with self.assertRaises(PluginError) as failure:
            PluginHost({'version': 1, 'plugins': [{'id': 'provider', 'use': 'models.service',
                'config': {'config_path': str(self.path)}}]}, model_plugins())
        self.assertEqual(failure.exception.code, 'INVALID_PLUGIN_CONFIG')
        self.assertFalse((self.root / 'budget.sqlite3').exists())

    def test_partial_provider_mount_failure_releases_registered_routes_and_owned_clients_once(self):
        profile, self.catalog = self.compiled()
        registry = ModelService()

        def adapter(configuration, name, ledger, **options):
            if name == 'embedding':
                raise RuntimeError('synthetic provider setup failure')
            return configured_adapter(configuration, name, ledger, **options)

        with patch('kb_platform.model_service.ModelService', return_value=registry), \
                patch('kb_platform.model_provider_plugins.configured_adapter', side_effect=adapter):
            with self.assertRaisesRegex(RuntimeError, 'synthetic provider setup failure'):
                PluginHost(profile, self.catalog)
        self.assertEqual(registry.catalog(), {})
        self.assertEqual(self.sent, [])
        self.assertGreater(len(self.clients), 0)
        for client in self.clients:
            client.close.assert_called_once_with()
            self.assertTrue(client.is_closed)

    def test_missing_configured_consumer_route_fails_before_budget_or_client_creation(self):
        profile = {'version': 1, 'plugins': [{'id': 'provider', 'use': 'models.service',
            'config': {'config_path': str(self.path)}},
            {'id': 'llm', 'use': 'models.structured', 'config': {'route': 'missing'}}]}
        with self.assertRaisesRegex(ValueError, 'MODEL_ROUTE_UNAVAILABLE'):
            compile_model_plugins(profile, model_plugins())
        self.assertFalse((self.root / 'budget.sqlite3').exists())
        self.assertEqual(self.clients, [])

    def test_arbitrary_consumer_declares_named_route_readiness_and_reloads_only_with_its_provider(self):
        mounted = []

        def consumer(ctx, config):
            bound = ctx.require('models').bind('alternative', Capability.CHAT)
            mounted.append(bound.registration_id)
            ctx.provide('thirdparty.selected', bound)

        spec = PluginSpec('thirdparty.selection', ('thirdparty.selected',),
                          ('models', model_route_marker('alternative')), consumer)
        profile = {'version': 1, 'plugins': [
            {'id': 'provider', 'use': 'models.service', 'config': {'config_path': str(self.path)}},
            {'id': 'thirdparty', 'use': 'thirdparty.selection'}]}
        profile, catalog = compile_model_plugins(profile, (*model_plugins(), spec))
        with PluginHost(profile, catalog) as host:
            selected = host.bind('thirdparty.selected')
            bound = host.service('thirdparty.selected')
            self.assertEqual(bound.invoke(self.request()).data['text'], 'chat-b')
            self.assertEqual(mounted, [bound.registration_id])
            self.configuration['models']['generation']['model'] = 'chat-a-reloaded'
            self.write_configuration()
            host.apply(profile)
            self.assertEqual(host.bind('thirdparty.selected').identity, selected.identity)
            self.assertEqual(mounted, [bound.registration_id])
            registry = host.service('models')
            independent = registry.bind('generation', Capability.CHAT)
            self.configuration['models']['alternative']['model'] = 'chat-b-reloaded'
            self.write_configuration()
            host.apply(profile)
            self.assertNotEqual(host.bind('thirdparty.selected').identity, selected.identity)
            self.assertEqual(len(mounted), 2)
            self.assertEqual(registry.bind('generation', Capability.CHAT).registration_id,
                             independent.registration_id)
            with self.assertRaisesRegex(RuntimeError, 'MODEL_BINDING_CHANGED'):
                bound.invoke(self.request())
            self.assertEqual(host.service('thirdparty.selected').invoke(self.request()).data['text'],
                             'chat-b-reloaded')
        self.assertEqual(len(self.sent), 2)

    def test_arbitrary_missing_route_dependency_fails_graph_validation_before_mount_effects(self):
        mounted = []
        spec = PluginSpec('thirdparty.selection', ('thirdparty.selected',),
            ('models', model_route_marker('missing-route')),
            lambda ctx, config: mounted.append(ctx.identity))
        profile = {'version': 1, 'plugins': [
            {'id': 'provider', 'use': 'models.service', 'config': {'config_path': str(self.path)}},
            {'id': 'thirdparty', 'use': 'thirdparty.selection'}]}
        profile, catalog = compile_model_plugins(profile, (*model_plugins(), spec))
        with self.assertRaises(PluginError) as failure:
            PluginHost(profile, catalog)
        self.assertEqual(failure.exception.code, 'missing_service')
        self.assertEqual(mounted, [])
        self.assertEqual(self.clients, [])
        self.assertEqual(self.sent, [])
        self.assertFalse((self.root / 'budget.sqlite3').exists())
