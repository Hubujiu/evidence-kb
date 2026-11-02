import ast
import inspect
import unittest

from kb_platform.llm_protocol import LLM_PROTOCOL, chat_metadata
from kb_platform.model_errors import ModelPreflightError, RemoteModelError
from kb_platform.model_sdk import Capability, ModelInfo, ModelRequest, ModelResult
from kb_platform.model_service import ModelService
from kb_platform.plugin_native import PluginError, native_call


class LlmRegistryContracts(unittest.TestCase):
    @staticmethod
    def call(operation, **fields):
        return native_call('llm_' + operation, protocol=LLM_PROTOCOL, **fields)

    def service(self):
        service = self.call('service_create')['service']
        self.addCleanup(self.close_service, service)
        row = self.call('route_register', service=service, name='chat', identity=None, info={
            'provider': 'fixture', 'model': 'same', 'capabilities': ['chat'], 'input_modalities': ['text'],
            'streaming': False, 'revision': '1', 'preferred_batch_size': 1, 'protocol': 'model-service/1'})['route']
        return service, row

    def close_service(self, service):
        rows = self.call('service_begin_close', service=service)['routes']
        for row in rows:
            if row['phase'] != 'cleaning':
                self.call('route_begin_cleanup', service=service, name=row['name'], binding=row['binding'])
            self.call('route_finish_cleanup', service=service, binding=row['binding'], error=None)
        self.call('service_finish_close', service=service)

    def prepare(self, service, row):
        return self.call('call_prepare', service=service, name=row['name'], capability='chat',
            expected=row['identity'], kind='invoke', request=chat_metadata('fixture', 'same',
            [{'role': 'user', 'content': 'synthetic'}], max_tokens=50))['prepared']

    def begin(self, service, prepared):
        call = self.call('call_begin', service=service, prepared=prepared)['call']
        self.addCleanup(self.release_if_active, service, call)
        return call

    def release_if_active(self, service, call):
        try:
            self.call('call_release', service=service, call=call)
        except PluginError as error:
            if error.code != 'LLM_CALL_UNAVAILABLE':
                raise

    @staticmethod
    def result(**changes):
        return {'provider': 'fixture', 'model': 'same', 'capability': 'chat', 'textIsText': True,
                'finishReason': 'stop', 'usage': None, **changes}

    def assert_code(self, code, operation, **fields):
        with self.assertRaises(PluginError) as caught:
            self.call(operation, **fields)
        self.assertEqual(caught.exception.code, code)

    def test_prepare_then_drain_cannot_admit_or_reuse_the_prepared_call(self):
        service, row = self.service()
        prepared = self.prepare(service, row)
        self.call('route_begin_cleanup', service=service, name='chat', binding=row['binding'])
        self.assert_code('LLM_BINDING_CHANGED', 'call_begin', service=service, prepared=prepared)
        self.assert_code('LLM_PREPARED_UNAVAILABLE', 'call_begin', service=service, prepared=prepared)
        state = self.call('service_inspect', service=service)
        self.assertEqual((state['prepared'], state['calls'], state['routes'][0]['activeCalls']), (0, 0, 0))

    def test_admitted_call_blocks_cleanup_and_may_finish_during_drain_then_releases_once(self):
        service, row = self.service()
        call = self.begin(service, self.prepare(service, row))
        self.assert_code('LLM_BUSY', 'route_begin_cleanup', service=service, name='chat', binding=row['binding'])
        self.assert_code('LLM_BUSY', 'service_begin_close', service=service)
        self.assert_code('LLM_PROVIDER_UNAVAILABLE', 'route_bind', service=service,
                         name='chat', capability='chat', expected=row['identity'])
        result = self.call('call_complete', service=service, call=call,
                           result=self.result(usage={'prompt_tokens': 12, 'completion_tokens': 4}))
        self.assertEqual(result['usage']['totalTokens'], 16)
        self.assert_code('LLM_CALL_FINISHED', 'call_complete', service=service, call=call, result=self.result())
        self.call('call_release', service=service, call=call)
        self.assert_code('LLM_CALL_UNAVAILABLE', 'call_release', service=service, call=call)
        snapshot = self.call('route_begin_cleanup', service=service, name='chat', binding=row['binding'])['route']
        self.assertEqual((snapshot['phase'], snapshot['activeCalls']), ('cleaning', 0))

    def test_equal_route_names_in_distinct_services_cannot_exchange_call_tickets(self):
        first, row_a = self.service()
        second, row_b = self.service()
        prepared_a, prepared_b = self.prepare(first, row_a), self.prepare(second, row_b)
        self.assertNotEqual(prepared_a, prepared_b)
        self.assert_code('LLM_PREPARED_UNAVAILABLE', 'call_begin', service=second, prepared=prepared_a)
        call_a, call_b = self.begin(first, prepared_a), self.begin(second, prepared_b)
        self.assertNotEqual(call_a, call_b)
        self.assert_code('LLM_CALL_UNAVAILABLE', 'call_complete', service=second, call=call_a, result=self.result())
        self.assert_code('LLM_CALL_UNAVAILABLE', 'call_release', service=second, call=call_a)
        self.assertEqual(self.call('service_inspect', service=second)['routes'][0]['activeCalls'], 1)
        self.assert_code('INVALID_LLM_RESPONSE', 'call_complete', service=first, call=call_a,
                         result=self.result(model='different'))
        for service, call in ((first, call_a), (second, call_b)):
            self.assertIsNone(self.call('call_complete', service=service, call=call, result=self.result())['usage'])

    def test_adoption_invalidates_old_prepared_calls_and_cleanup_authority(self):
        service, previous = self.service()
        prepared = self.prepare(service, previous)
        identity = {'registrationId': 'new-owner', 'owner': {'context': 7, 'generation': 2}}
        current = self.call('route_adopt', service=service, identities=[{
            'registration': previous['binding']['registration'], 'identity': identity}])['routes'][0]
        self.assertEqual(current['binding']['registration'], previous['binding']['registration'])
        self.assertNotEqual(current['binding']['generation'], previous['binding']['generation'])
        self.assert_code('LLM_PREPARED_UNAVAILABLE', 'call_begin', service=service, prepared=prepared)
        self.assertIsNone(self.call('route_begin_cleanup', service=service, name='chat',
                                   binding=previous['binding'])['route'])
        self.assert_code('LLM_BINDING_CHANGED', 'route_bind', service=service,
                         name='chat', capability='chat', expected=previous['identity'])
        self.assertEqual(self.call('route_bind', service=service, name='chat',
                                   capability='chat', expected=identity)['phase'], 'active')
        self.call('route_begin_cleanup', service=service, name='chat', binding=current['binding'])
        self.assert_code('LLM_BINDING_CHANGED', 'route_finish_cleanup', service=service,
                         binding=previous['binding'], error=None)

    def test_cleanup_failure_stays_visible_and_retries_the_same_generation(self):
        service, row = self.service()
        self.call('route_begin_cleanup', service=service, name='chat', binding=row['binding'])
        self.call('route_finish_cleanup', service=service, binding=row['binding'], error='SyntheticFailure')
        state = self.call('service_inspect', service=service)['routes'][0]
        self.assertEqual((state['phase'], state['cleanupError']), ('cleanup_failed', 'SyntheticFailure'))
        self.assertEqual(state['binding'], row['binding'])
        self.call('route_begin_cleanup', service=service, name='chat', binding=row['binding'])

    def test_python_provider_and_postflight_failures_release_native_admission(self):
        class Provider:
            info = ModelInfo('fixture', 'same', frozenset({Capability.CHAT}))

            def invoke(self, request):
                if isinstance(self.outcome, BaseException):
                    raise self.outcome
                return self.outcome

        models, provider = ModelService(), Provider()
        self.addCleanup(models.close)
        models.register('chat', provider)
        port = models.completion_port('chat')
        for outcome in (ModelPreflightError('SYNTHETIC_PREFLIGHT'), RemoteModelError('SYNTHETIC_FAILURE'),
                        ModelResult('wrong', 'same', Capability.CHAT, {'text': 'invalid identity'})):
            with self.subTest(outcome=type(outcome).__name__):
                provider.outcome = outcome
                with self.assertRaises((ValueError, RuntimeError)):
                    port.complete([{'role': 'user', 'content': 'synthetic'}], 20,
                                  purpose='contract', key='fixture', trace={'attempt': 0})
                self.assertEqual(models.inspect()['chat']['active_calls'], 0)
        provider.outcome = ModelResult('fixture', 'same', Capability.CHAT, {'text': 'accepted'})
        self.assertEqual(models.invoke('chat', ModelRequest(Capability.CHAT,
                         {'messages': [{'role': 'user', 'content': 'synthetic'}]})).data['text'], 'accepted')

    def test_python_model_service_cannot_restore_a_parallel_route_state_machine(self):
        tree = ast.parse(inspect.getsource(ModelService))
        stored = {node.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)}
        self.assertFalse(stored & {'_entries', '_issued_ids', '_state', 'phase', 'active', 'cleanup_error'})
        self.assertIn('_adapters', stored)

    def test_postflight_identity_and_native_validation_failures_preserve_observed_usage(self):
        class Provider:
            info = ModelInfo('fixture', 'same', frozenset({Capability.CHAT}))

            def invoke(self, request):
                return self.result

        models, provider = ModelService(), Provider()
        self.addCleanup(models.close)
        models.register('chat', provider)
        port = models.completion_port('chat')
        messages = [{'role': 'user', 'content': 'synthetic'}]
        usage = {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17}
        for identity, finish in (('wrong', 'stop'), ('fixture', {'invalid': 'finish'})):
            provider.result = ModelResult(identity, 'same', Capability.CHAT,
                                          {'text': 'received', 'finish_reason': finish}, usage,
                                          request_id='logical-fixture', physical_request_id='physical-fixture')
            for path, invoke in (
                    ('direct', lambda: models.invoke('chat', ModelRequest(Capability.CHAT, {'messages': messages}))),
                    ('port', lambda: port.complete(messages, 30, purpose='contract', key='fixture', trace={'attempt': 0}))):
                with self.subTest(identity=identity, finish=finish, path=path):
                    with self.assertRaises(RemoteModelError) as caught:
                        invoke()
                    self.assertIs(caught.exception.model_sent, True)
                    self.assertEqual(caught.exception.observed_usage, usage)
                    self.assertEqual(caught.exception.request_id, 'logical-fixture')
                    self.assertEqual(caught.exception.physical_request_id, 'physical-fixture')
                    self.assertEqual(models.inspect()['chat']['active_calls'], 0)


if __name__ == '__main__':
    unittest.main()
