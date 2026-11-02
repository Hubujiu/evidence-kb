import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.contract_transfer import MAX_MESSAGE_BYTES, MAX_TRANSFERS
from kb_platform.llm_contract import LlmClient, LlmEndpoint, METHODS
from kb_platform.model_adapters import LunaAdapter
from kb_platform.model_errors import ModelPreflightError, RemoteModelError
from kb_platform.model_sdk import Capability, ModelInfo, ModelRequest, ModelResult
from kb_platform.model_service import ModelService
from kb_platform.plugin_contracts import ServiceEndpoint

from .llm_contract_contracts import Adapter


USAGE = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}


class LlmWireOutcomeContracts(unittest.TestCase):
    def service(self, adapter, transform=None):
        service = ModelService()
        remove = service.register('selected', adapter)
        self.addCleanup(service.close)
        endpoint = LlmEndpoint(service)
        self.addCleanup(endpoint.close)
        public = ServiceEndpoint(endpoint, METHODS)

        def exchange(request):
            response = public.exchange(json.loads(json.dumps(request)))
            response = transform(response) if transform is not None else response
            return json.loads(json.dumps(response))

        client = LlmClient(SimpleNamespace(catalog=public.catalog, exchange=exchange))
        return service, remove, client, public

    def request(self):
        return ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': 'hello'}]})

    def assert_observed(self, error, calls, code, *, request_id='logical-provider-request',
                        physical_request_id='observed-physical-request'):
        self.assertIsInstance(error, RemoteModelError)
        self.assertNotIsInstance(error, ModelPreflightError)
        self.assertEqual(error.code, code)
        self.assertIs(error.model_sent, True)
        self.assertEqual(error.observed_usage, USAGE)
        self.assertEqual(error.request_id, request_id)
        self.assertEqual(getattr(error, 'physical_request_id', None), physical_request_id)
        self.assertEqual(len(calls), 1)

    def test_server_upload_capacity_failure_preserves_zero_send_and_zero_attempts(self):
        adapter = Adapter()
        _, _, client, public = self.service(adapter)
        port = client.structured('selected').provider
        for _ in range(MAX_TRANSFERS):
            public.exchange({'version': 1, 'op': 'wire_begin', 'bytes': 5,
                             'sha256': '0' * 64, 'transaction': None})
        trace = {'attempt': 0}
        with self.assertRaises(ModelPreflightError) as raised:
            port.complete([{'role': 'user', 'content': 'x' * (MAX_MESSAGE_BYTES + 100)}], 1,
                          purpose='capacity-contract', key='capacity-contract', trace=trace)
        self.assertEqual(raised.exception.code, 'CONTRACT_WIRE_TRANSFER_LIMIT')
        self.assertIs(raised.exception.model_sent, False)
        self.assertEqual(trace['attempt'], 0)
        self.assertEqual(adapter.requests, [])

    def test_reserved_response_field_cannot_turn_a_completed_invoke_into_unsent(self):
        calls = []

        def invoke(request):
            calls.append(request)
            return ModelResult('synthetic', 'contract-model', request.capability,
                {'text': 'provider completed', '$media': 'reserved field'}, USAGE, 'logical-provider-request',
                physical_request_id='observed-physical-request')

        service, _, client, _ = self.service(Adapter(invoke=invoke))
        with self.assertRaises(RemoteModelError) as raised:
            client.invoke('selected', self.request())
        self.assert_observed(raised.exception, calls, 'MODEL_WIRE_RESERVED_KEY')
        self.assertEqual(service.inspect()['selected']['active_calls'], 0)

        luna_calls = []

        def post(path, payload, headers, purpose, key, *, trace):
            trace['attempt'] += 1
            luna_calls.append(dict(trace))
            return {'choices': [{'message': {'content': 'provider completed'}, 'finish_reason': 'stop'}],
                    'usage': USAGE}

        luna = LunaAdapter(SimpleNamespace(provider_id='synthetic', model='contract-model', retries=0,
            base_url='https://synthetic.invalid', _headers={}, _post=post))

        def luna_invoke(request):
            result = luna.invoke(request)
            return replace(result, data={**result.data, '$media': 'reserved field'})

        luna_service, _, luna_client, _ = self.service(Adapter(invoke=luna_invoke))
        luna_request = replace(self.request(), call_context={'call_id': 'logical-luna-request'})
        with self.assertRaises(RemoteModelError) as luna_raised:
            luna_client.invoke('selected', luna_request)
        self.assert_observed(luna_raised.exception, luna_calls, 'MODEL_WIRE_RESERVED_KEY',
                             request_id='logical-luna-request', physical_request_id=None)
        self.assertEqual(luna_service.inspect()['selected']['active_calls'], 0)

    def test_client_decode_failure_keeps_observed_response_facts(self):
        calls = []

        def invoke(request):
            calls.append(request)
            return ModelResult('synthetic', 'contract-model', request.capability,
                {'text': 'provider completed', 'finish_reason': 'stop'}, USAGE, 'logical-provider-request',
                physical_request_id='observed-physical-request')

        def corrupt(response):
            response['value']['data'] = {'$media': {'mime_type': 'image/png', 'base64': '!!!'}}
            return response

        service, _, client, _ = self.service(Adapter(invoke=invoke), transform=corrupt)
        with self.assertRaises(RemoteModelError) as raised:
            client.invoke('selected', self.request())
        self.assert_observed(raised.exception, calls, 'INVALID_MODEL_MEDIA')
        self.assertEqual(service.inspect()['selected']['active_calls'], 0)

    def test_catalog_replacement_does_not_mix_previous_info_with_current_registration(self):
        original, replacement = Adapter(), Adapter()
        replacement.info = ModelInfo('replacement-provider', 'replacement-model',
            frozenset({Capability.CHAT}), revision='replacement-v2')
        service, remove, client, _ = self.service(original)
        catalog = service.catalog

        def replace_after_snapshot():
            snapshot = catalog()
            remove()
            service.register('selected', replacement)
            return snapshot

        with patch.object(service, 'catalog', side_effect=replace_after_snapshot):
            descriptor = client.describe()['selected']
        self.assertEqual(descriptor['info']['provider'], replacement.info.provider)
        self.assertEqual(descriptor['info']['model'], replacement.info.model)
        self.assertEqual(descriptor['info']['revision'], replacement.info.revision)
        self.assertEqual(descriptor['registration'], service.bind('selected', Capability.CHAT).registration_id)
        result = client.invoke('selected', self.request())
        self.assertEqual(result.provider, replacement.info.provider)
        self.assertEqual(result.model, replacement.info.model)
        self.assertEqual(original.requests, [])
        self.assertEqual(len(replacement.requests), 1)
