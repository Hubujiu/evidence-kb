from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.agent_model import BudgetLedger
from kb_platform.agent_provider import ProviderCompletion
from kb_platform.contract_transfer import MAX_MESSAGE_BYTES, TransferError
from kb_platform.knowledge_contracts import ModelEndpoint, domain_call
from kb_platform.llm_contract import LlmClient, LlmEndpoint, METHODS
from kb_platform.model_errors import ModelPreflightError, RemoteModelError, TerminalContractError
from kb_platform.model_sdk import Capability, Media, ModelInfo, ModelRequest, ModelResult
from kb_platform.model_service import ModelService
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint


class Adapter:
    max_attempts = 1

    def __init__(self, *, invoke=None, complete=None):
        self.info = ModelInfo('synthetic', 'contract-model', frozenset({Capability.CHAT,
                              Capability.EMBEDDING, Capability.OCR}), frozenset({'text', 'image'}))
        self.requests, self.cached = [], {}
        self._invoke, self._complete = invoke, complete

    def invoke(self, request):
        if self._invoke is not None:
            return self._invoke(request)
        self.requests.append(request)
        return ModelResult(self.info.provider, self.info.model, request.capability,
                           {'text': request.payload['messages'][0]['content'], 'finish_reason': 'stop'})

    def completion_port(self):
        return self

    def identity(self, messages, output_limit):
        return {'provider': self.info.provider, 'model': self.info.model,
                'messages': messages, 'output_limit': output_limit}

    def complete(self, messages, output_limit, *, purpose, key, trace):
        trace['attempt'] += 1
        self.requests.append(deepcopy(messages))
        if self._complete is not None:
            return self._complete(trace)
        return ProviderCompletion('{"value":42}', 'stop', {
            'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})

    def cache_get(self, key):
        return self.cached.get(key)

    def cache_put(self, key, value):
        self.cached[key] = deepcopy(value)

    def record(self, purpose, key, **metadata):
        pass

    def save_failed_response(self, result, metadata):
        return {'response_persisted': False}


class LlmContractContracts(unittest.TestCase):
    def service(self, adapter, *, policy=None):
        service = ModelService()
        remove = service.register('selected', adapter)
        self.addCleanup(service.close)
        endpoint = LlmEndpoint(service, policy=policy)
        self.addCleanup(endpoint.close)
        public = ServiceEndpoint(endpoint, METHODS)
        frames = []

        def exchange(request):
            serialized = json.dumps(request)
            frames.append(len(serialized.encode()))
            response = public.exchange(json.loads(serialized))
            frames.append(len(json.dumps(response).encode()))
            return response

        client = LlmClient(SimpleNamespace(catalog=public.catalog, exchange=exchange))
        return service, remove, client, endpoint, frames

    def test_public_completion_preserves_validation_receipt_and_actual_attempts(self):
        adapter = Adapter()
        _, _, client, _, _ = self.service(adapter)
        model = client.structured('selected')
        messages = [{'role': 'user', 'content': 'Synthetic value?'}]
        options = {'role': 'test', 'schema': {'type': 'object', 'required': ['value']}, 'call_context': {}}
        envelope = model.complete(messages, **options, cache_result=False)
        self.assertEqual(envelope.value, {'value': 42})
        self.assertEqual(envelope.attempts, 1)
        self.assertEqual(envelope.usage['total_tokens'], 15)
        self.assertEqual(adapter.cached, {})
        model.accept_completion(envelope)
        reused = model.complete(messages, **options)
        self.assertEqual(reused.attempts, 0)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(model.ledger.snapshot()['calls'], 1)

    def test_registry_replacement_invalidates_public_prepared_binding_before_send(self):
        adapter = Adapter()
        registry, remove, client, _, _ = self.service(adapter)
        prepared = client.structured('selected')
        remove()
        replacement = Adapter()
        registry.register('selected', replacement)
        with self.assertRaisesRegex(ModelPreflightError, 'MODEL_BINDING_CHANGED') as caught:
            prepared.complete([{'role': 'user', 'content': 'value'}], role='test', schema={}, call_context={})
        self.assertFalse(caught.exception.model_sent)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(replacement.requests, [])

    def test_unknown_completion_preserves_physical_id_trace_and_reserved_usage(self):
        def fail(trace):
            error = RemoteModelError('MODEL_HTTP_UNKNOWN')
            error.model_sent, error.physical_request_id = True, 'physical-observed-once'
            raise error

        adapter = Adapter(complete=fail)
        _, _, client, _, _ = self.service(adapter)
        ledger = BudgetLedger(max_calls=2, max_tokens=100000)
        model = client.structured('selected', ledger=ledger)
        with self.assertRaises(RemoteModelError) as caught:
            model.complete([{'role': 'user', 'content': 'value'}], role='test', schema={}, call_context={})
        error = caught.exception
        self.assertEqual(error.physical_request_id, 'physical-observed-once')
        self.assertTrue(error.model_sent)
        self.assertEqual(error.call_record['attempts'], 1)
        self.assertFalse(error.call_record['usage']['available'])
        self.assertEqual(ledger.snapshot()['unknown_usage_calls'], 1)
        self.assertGreater(ledger.snapshot()['accounted_tokens'], 0)
        self.assertEqual(len(adapter.requests), 1)

    def test_large_text_request_and_result_share_bounded_transport_without_resending(self):
        adapter = Adapter()
        _, _, client, endpoint, frames = self.service(adapter)
        text = '原文🚀' * 550000
        result = client.invoke('selected', ModelRequest(Capability.CHAT,
            {'messages': [{'role': 'user', 'content': text}], 'max_tokens': 10}))
        self.assertEqual(result.data['text'], text)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.requests[0].payload['messages'][0]['content'], text)
        self.assertGreater(len(frames), 10)
        self.assertLess(max(frames), MAX_MESSAGE_BYTES)
        self.assertEqual(endpoint._wire._transfers, {})

    def test_ocr_policy_runs_inside_provider_before_cache_for_large_media(self):
        received, cached = [], []
        policy = SimpleNamespace(admit=lambda request: {'allowed': True})

        def validate(request):
            received.append(request)
            raise ContractError('OCR_INVALID_COORDINATES', 'Invalid coordinates', status_code=422)

        policy.validate = validate

        def invoke(request):
            self.assertEqual(request.payload['media'].data, data)
            value = {'blocks': [{'bbox': [1, 0, 0, 1]}]}
            request.payload['validator'](value)
            cached.append(value)
            raise AssertionError('Invalid model output must not be cached')

        data = b'\x89PNG' + b'x' * (5 * 1024 * 1024)
        _, _, client, endpoint, frames = self.service(Adapter(invoke=invoke), policy=policy)
        with self.assertRaisesRegex(RemoteModelError, 'OCR_INVALID_COORDINATES'):
            client.invoke('selected', ModelRequest(Capability.OCR, {
                'media': Media(data, 'image/png'), 'contract_version': 'ocr-blocks/1'}), validation='ocr-blocks/1')
        self.assertEqual(len(received), 1)
        self.assertEqual(cached, [])
        self.assertLess(max(frames), MAX_MESSAGE_BYTES)
        self.assertEqual(endpoint._wire._transfers, {})

    def test_embedding_rechecks_public_policy_at_physical_admission(self):
        checks, sends = [], []

        def admit(request):
            checks.append(request)
            return {'allowed': len(checks) == 1}

        def invoke(request):
            if request.call_context['authorize']() is not True:
                raise ModelPreflightError('EMBEDDING_AUTHORIZATION_FAILED')
            sends.append(request)
            return ModelResult('synthetic', 'contract-model', Capability.EMBEDDING, {'vectors': [[1.0]]})

        _, _, client, _, _ = self.service(Adapter(invoke=invoke),
            policy=SimpleNamespace(admit=admit, validate=lambda request: {}))
        with self.assertRaises(ModelPreflightError) as caught:
            client.invoke('selected', ModelRequest(Capability.EMBEDDING, {'texts': ['source'], 'dimensions': 1},
                {'purpose': 'embedding_document', 'audit_context': {'revision': 'original'}}))
        self.assertFalse(caught.exception.model_sent)
        self.assertEqual(len(checks), 2)
        self.assertEqual(sends, [])

    def test_public_operation_schema_rejects_unknown_fields_before_adapter(self):
        adapter = Adapter()
        _, _, client, endpoint, _ = self.service(adapter)
        binding = client.bind('selected', Capability.CHAT)
        with self.assertRaises(ContractError) as caught:
            endpoint.exchange({'version': 1, 'op': 'invoke', 'route': 'selected',
                'registration': binding.registration_id, 'capability': 'chat',
                'payload': {}, 'context': {}, 'validation': None, 'fallback': 'another-provider'})
        self.assertFalse(caught.exception.details['model_sent'])
        self.assertEqual(adapter.requests, [])

    def test_python_callbacks_cannot_be_silently_weakened_to_remote_policy(self):
        adapter = Adapter()
        _, _, client, _, _ = self.service(adapter)
        requests = [ModelRequest(Capability.OCR, {'media': Media(b'fixture', 'image/png'),
                        'validator': lambda value: None, 'contract_version': 'ocr-blocks/1'}),
                    ModelRequest(Capability.EMBEDDING, {'texts': ['private']},
                        {'authorize': lambda: True})]
        for request in requests:
            with self.subTest(capability=request.capability):
                with self.assertRaisesRegex(ModelPreflightError, 'MODEL_CALLBACK_NOT_PORTABLE') as caught:
                    client.invoke('selected', request)
                self.assertFalse(caught.exception.model_sent)
        self.assertEqual(adapter.requests, [])

    def test_request_transfer_limit_is_known_unsent_and_does_not_charge_budget(self):
        adapter = Adapter()
        _, _, client, _, _ = self.service(adapter)
        model = client.structured('selected')
        with patch('kb_platform.contract_transfer.MAX_VALUE_BYTES', 4096):
            with self.assertRaises(ModelPreflightError) as caught:
                model.complete([{'role': 'user', 'content': 'x' * 5000}],
                    role='test', schema={}, call_context={})
        self.assertFalse(caught.exception.model_sent)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(model.ledger.snapshot()['calls'], 0)
        self.assertEqual(model.ledger.snapshot()['reserved_calls'], 0)

    def test_result_publication_failure_keeps_observed_usage_and_never_replays(self):
        adapter = Adapter(complete=lambda trace: ProviderCompletion(json.dumps({'value': 'x' * 20000}),
            'stop', {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}))
        _, _, client, endpoint, _ = self.service(adapter)
        model = client.structured('selected')
        with patch('kb_platform.contract_transfer.MAX_VALUE_BYTES', 4096):
            with self.assertRaisesRegex(RemoteModelError, 'MODEL_RESULT_UNAVAILABLE') as caught:
                model.complete([{'role': 'user', 'content': 'value'}], role='test', schema={}, call_context={})
        self.assertTrue(caught.exception.model_sent)
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertTrue(caught.exception.call_record['usage']['available'])
        self.assertEqual(caught.exception.call_record['usage']['total_tokens'], 15)
        self.assertEqual(model.ledger.snapshot()['calls'], 1)
        self.assertEqual(model.ledger.snapshot()['tokens'], 15)
        self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 0)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.cached, {})
        self.assertEqual(endpoint._wire._transfers, {})

    def test_closed_endpoint_rejects_completion_before_adapter_and_attempt(self):
        adapter = Adapter()
        _, _, client, endpoint, _ = self.service(adapter)
        port = client.structured('selected').provider
        endpoint.close()
        trace = {'attempt': 0}
        with self.assertRaisesRegex(ModelPreflightError, 'CONTRACT_WIRE_CLOSED') as caught:
            port.complete([{'role': 'user', 'content': 'value'}], 10,
                          purpose='test', key='closed', trace=trace)
        self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(trace['attempt'], 0)
        self.assertEqual(adapter.requests, [])

    def test_lost_rpc_result_does_not_invent_a_provider_send_fact_or_replay(self):
        adapter = Adapter()
        _, _, client, _, _ = self.service(adapter)
        model = client.structured('selected')
        send = client._wire._send

        def lose_result(request):
            result = send(request)
            if request['op'] == 'complete':
                raise TransferError('CONTRACT_WIRE_RESULT_LOST')
            return result

        with patch.object(client._wire, '_send', side_effect=lose_result):
            with self.assertRaisesRegex(RemoteModelError, 'CONTRACT_WIRE_RESULT_LOST') as caught:
                model.complete([{'role': 'user', 'content': 'value'}], role='test', schema={}, call_context={})
        self.assertIsNone(getattr(caught.exception, 'model_sent', None))
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertFalse(caught.exception.call_record['usage']['available'])
        self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 1)
        self.assertGreater(model.ledger.snapshot()['accounted_tokens'], 0)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.cached, {})

    def test_download_failure_cannot_reclassify_completed_operation_as_unsent(self):
        adapter = Adapter(complete=lambda trace: ProviderCompletion(json.dumps({'value': 'x' * 20000}),
            'stop', {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}))
        _, _, client, endpoint, _ = self.service(adapter)
        model = client.structured('selected')
        send = client._wire._send

        def close_after_publication(request):
            result = send(request)
            if request['op'] == 'complete':
                endpoint.close()
            return result

        with patch.object(client._wire, '_send', side_effect=close_after_publication), \
                patch('kb_platform.contract_transfer.MAX_MESSAGE_BYTES', 4096):
            with self.assertRaisesRegex(RemoteModelError, 'CONTRACT_WIRE_CLOSED') as caught:
                model.complete([{'role': 'user', 'content': 'value'}], role='test', schema={}, call_context={})
        self.assertIsNone(getattr(caught.exception, 'model_sent', None))
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 1)
        self.assertGreater(model.ledger.snapshot()['accounted_tokens'], 0)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.cached, {})

    def test_invalid_request_is_known_unsent_at_public_and_direct_boundaries(self):
        adapter = Adapter()
        service, _, client, _, _ = self.service(adapter)
        request = ModelRequest(Capability.CHAT, {'messages': [], 'max_tokens': 10})
        for invoke in (lambda: service.invoke('selected', request), lambda: client.invoke('selected', request)):
            with self.subTest(invoke=invoke):
                with self.assertRaisesRegex(ModelPreflightError, 'INVALID_MODEL_MESSAGES') as caught:
                    invoke()
                self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(adapter.requests, [])

    def test_post_completion_cache_refusal_retains_model_result_facts(self):
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        adapter = Adapter(complete=lambda trace: ProviderCompletion('{"value":42}', 'stop', usage,
            physical_request_id='completed-before-cache-refusal'))
        _, _, client, _, _ = self.service(adapter)
        model = client.structured('selected')
        with patch.object(adapter, 'cache_put', side_effect=ModelPreflightError('CACHE_WRITE_DENIED')):
            with self.assertRaisesRegex(ModelPreflightError, 'CACHE_WRITE_DENIED') as caught:
                model.complete([{'role': 'user', 'content': 'value'}], role='test', schema={}, call_context={})
        self.assertIs(caught.exception.model_sent, True)
        self.assertIsNone(caught.exception.preflight_code)
        self.assertEqual(caught.exception.observed_usage, usage)
        self.assertEqual(caught.exception.call_record['physical_request_id'], 'completed-before-cache-refusal')
        self.assertIs(caught.exception.call_record['model_sent'], True)
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertEqual(model.ledger.snapshot()['tokens'], 15)
        self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 0)
        self.assertEqual(len(adapter.requests), 1)

    def test_terminal_failure_keeps_code_and_usage_through_knowledge_contract(self):
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}

        def fail(trace):
            error = TerminalContractError('TERMINAL_OUTPUT_REJECTED')
            error.model_sent, error.observed_usage = True, usage
            error.physical_request_id = 'terminal-physical-request'
            raise error

        adapter = Adapter(complete=fail)
        _, _, client, _, _ = self.service(adapter)
        model = client.structured('selected')
        endpoint = ModelEndpoint(model)
        self.addCleanup(endpoint.close)
        with self.assertRaises(TerminalContractError) as caught:
            domain_call(endpoint.complete, {'messages': [{'role': 'user', 'content': 'value'}],
                'role': 'test', 'schema': {}, 'call_context': {}, 'output_limit': 10})
        self.assertEqual(caught.exception.code, 'TERMINAL_OUTPUT_REJECTED')
        self.assertIs(caught.exception.model_sent, True)
        self.assertEqual(caught.exception.observed_usage, usage)
        self.assertEqual(caught.exception.physical_request_id, 'terminal-physical-request')
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertEqual(model.ledger.snapshot()['tokens'], 15)
        self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 0)
        self.assertEqual(len(adapter.requests), 1)

    def test_application_budget_refusal_is_known_unsent(self):
        adapter = Adapter()
        _, _, client, _, _ = self.service(adapter)
        model = client.structured('selected', ledger=BudgetLedger(max_tokens=1))
        endpoint = ModelEndpoint(model)
        self.addCleanup(endpoint.close)
        with self.assertRaisesRegex(RemoteModelError, 'AGENT_TOKEN_BUDGET_EXHAUSTED') as caught:
            domain_call(endpoint.complete, {'messages': [{'role': 'user', 'content': 'value'}],
                'role': 'test', 'schema': {}, 'call_context': {}, 'output_limit': 10})
        self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(model.ledger.snapshot()['calls'], 0)
        self.assertEqual(model.ledger.snapshot()['unknown_usage_calls'], 0)
        self.assertEqual(adapter.requests, [])
