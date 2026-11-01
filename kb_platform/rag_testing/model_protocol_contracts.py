from dataclasses import replace
from types import SimpleNamespace
import unittest

from kb_platform.agent_model import BudgetLedger
from kb_platform.model_adapters import LunaAdapter
from kb_platform.model_errors import ModelPreflightError
from kb_platform.model_sdk import Capability, Media, ModelAdapter, ModelInfo, ModelRequest, ModelResult
from kb_platform.model_service import ModelService


class EchoAdapter:
    info = ModelInfo('fixture', 'echo', frozenset({Capability.CHAT}))

    def __init__(self):
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return ModelResult('fixture', 'echo', Capability.CHAT,
                           {'text': '{"answer":"accepted"}', 'finish_reason': 'stop'})


class ModelProtocolContracts(unittest.TestCase):
    def setUp(self):
        self.service = ModelService()
        self.addCleanup(self.service.close)

    @staticmethod
    def request():
        return ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': 'question'}]})

    def test_registration_rejects_version_and_noncallable_before_publishing(self):
        for adapter, error in (
            (SimpleNamespace(info=replace(EchoAdapter.info, protocol='model-service/2'), invoke=lambda request: None),
             'MODEL_PROTOCOL_UNSUPPORTED'),
            (SimpleNamespace(info=EchoAdapter.info), 'INVALID_MODEL_ADAPTER_INVOKE'),
            (SimpleNamespace(info=EchoAdapter.info, invoke=42), 'INVALID_MODEL_ADAPTER_INVOKE'),
            (SimpleNamespace(info=EchoAdapter.info, invoke=lambda: None), 'INVALID_MODEL_ADAPTER_INVOKE'),
            (SimpleNamespace(info=EchoAdapter.info, invoke=lambda request, extra: None), 'INVALID_MODEL_ADAPTER_INVOKE'),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    self.service.register('generation', adapter)
                self.assertEqual(self.service.catalog(), {})

    def test_async_generator_and_inherited_protocol_stub_are_rejected(self):
        async def asynchronous(request):
            return None
        def generator(request):
            yield None
        class Incomplete(ModelAdapter):
            info = EchoAdapter.info
        adapters = [SimpleNamespace(info=EchoAdapter.info, invoke=callback)
                    for callback in (asynchronous, generator)] + [Incomplete()]
        for adapter in adapters:
            with self.subTest(adapter=type(adapter).__name__):
                with self.assertRaisesRegex(ValueError, 'INVALID_MODEL_ADAPTER_INVOKE'):
                    self.service.register('generation', adapter)
        self.assertEqual(self.service.catalog(), {})

    def test_owned_adapter_requires_callable_cleanup_but_borrowed_does_not(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_MODEL_ADAPTER_CLOSE'):
            self.service.register('generation', EchoAdapter(), owned=True)
        self.assertEqual(self.service.catalog(), {})
        self.service.register('generation', EchoAdapter())
        self.assertEqual(self.service.invoke('generation', self.request()).data['finish_reason'], 'stop')

    def test_cleanup_must_complete_synchronously_before_disposal(self):
        completed = []
        async def delayed_close():
            completed.append(True)
        adapter = EchoAdapter()
        adapter.close = lambda: delayed_close()
        dispose = self.service.register('generation', adapter, owned=True)
        with self.assertRaisesRegex(ValueError, 'INVALID_MODEL_CLEANUP_RESULT'):
            dispose()
        self.assertEqual(completed, [])
        self.assertEqual(self.service.inspect()['generation']['phase'], 'cleanup_failed')
        adapter.close = lambda: None
        dispose()
        self.assertEqual(self.service.catalog(), {})

    def test_invalid_optional_completion_factory_blocks_activation(self):
        adapter = EchoAdapter()
        adapter.completion_port = lambda required: None
        with self.assertRaisesRegex(ValueError, 'INVALID_MODEL_COMPLETION_PORT'):
            self.service.register('generation', adapter)
        self.assertEqual(self.service.catalog(), {})

    def test_invalid_completion_port_is_rejected_during_binding_without_a_call(self):
        adapter = EchoAdapter()
        adapter.completion_port = lambda: object()
        self.service.register('generation', adapter)
        with self.assertRaisesRegex(ValueError, 'INVALID_MODEL_COMPLETION_PORT'):
            self.service.structured('generation')
        self.assertEqual(adapter.requests, [])
        self.assertEqual(self.service.inspect()['generation']['active_calls'], 0)

    def test_duplicate_route_keeps_existing_binding_and_never_calls_new_adapter(self):
        adapter, replacement = EchoAdapter(), EchoAdapter()
        self.service.register('generation', adapter)
        with self.assertRaisesRegex(ValueError, 'MODEL_REGISTRATION_UNAVAILABLE'):
            self.service.register('generation', replacement)
        self.service.invoke('generation', self.request())
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(replacement.requests, [])

    def test_standard_adapter_supports_structured_calls_and_invalidated_bindings(self):
        adapter = EchoAdapter()
        remove = self.service.register('generation', adapter)
        model = self.service.structured('generation')
        options = {'role': 'answer', 'schema': {'type': 'object', 'required': ['answer'],
                   'properties': {'answer': {'type': 'string'}}}, 'call_context': {}}
        result = model.complete(self.request().payload['messages'], **options)
        self.assertEqual(result.value, {'answer': 'accepted'})
        self.assertEqual(result.attempts, 1)
        self.assertIn('Agent output contract', adapter.requests[0].payload['messages'][0]['content'])
        remove()
        self.service.register('generation', adapter)
        with self.assertRaisesRegex(RuntimeError, 'MODEL_BINDING_CHANGED'):
            model.complete(self.request().payload['messages'], **options)
        self.assertEqual(len(adapter.requests), 1)

    def test_structured_calls_validate_adapter_result_and_preserve_unknown_usage(self):
        adapter = EchoAdapter()
        adapter.invoke = lambda request: ModelResult('wrong-provider', 'echo', Capability.CHAT,
                                                     {'text': '{}', 'finish_reason': 'stop'})
        self.service.register('generation', adapter)
        budget = BudgetLedger(max_calls=2)
        model = self.service.structured('generation', ledger=budget)
        with self.assertRaisesRegex(RuntimeError, 'INVALID_MODEL_RESULT_IDENTITY') as caught:
            model.complete(self.request().payload['messages'], role='answer', schema={'type': 'object'},
                           call_context={})
        self.assertIs(caught.exception.model_sent, True)
        self.assertEqual(budget.snapshot()['calls'], 1)
        self.assertEqual(budget.snapshot()['unknown_usage_calls'], 1)

    def test_explicit_presend_failure_keeps_zero_usage(self):
        def reject(request):
            raise ModelPreflightError('FIXTURE_PREFLIGHT')
        adapter = EchoAdapter()
        adapter.invoke = reject
        self.service.register('generation', adapter)
        budget = BudgetLedger(max_calls=1)
        model = self.service.structured('generation', ledger=budget)
        with self.assertRaisesRegex(ValueError, 'FIXTURE_PREFLIGHT'):
            model.complete(self.request().payload['messages'], role='answer', schema={'type': 'object'},
                           call_context={})
        self.assertEqual(budget.snapshot()['calls'], 0)
        self.assertEqual(budget.snapshot()['unknown_usage_calls'], 0)

    def test_unknown_options_and_missing_capability_do_not_invoke_adapter(self):
        adapter = EchoAdapter()
        self.service.register('generation', adapter)
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_MODEL_OPTION'):
            self.service.invoke('generation', ModelRequest(Capability.CHAT,
                {**self.request().payload, 'stop': ['end']}))
        with self.assertRaisesRegex(RuntimeError, 'MODEL_CAPABILITY_UNAVAILABLE'):
            self.service.invoke('generation', ModelRequest(Capability.EMBEDDING, {'texts': ['query']}))
        self.assertEqual(adapter.requests, [])

    def test_luna_ocr_rejects_unhonored_output_limit_before_calling_client(self):
        client = SimpleNamespace(provider_id='fixture', model='echo',
                                 complete_json=lambda *args, **kwargs: self.fail('unexpected call'))
        self.service.register('ocr', LunaAdapter(client))
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_MODEL_OPTION'):
            self.service.invoke('ocr', ModelRequest(Capability.OCR,
                {'media': Media(b'synthetic', 'image/png'), 'max_tokens': 12}))


if __name__ == '__main__':
    unittest.main()
