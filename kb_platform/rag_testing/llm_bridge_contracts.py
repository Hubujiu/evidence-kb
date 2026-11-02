from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.model_adapters import LunaAdapter
from kb_platform.model_errors import ModelPreflightError
from kb_platform.model_sdk import Capability, ModelInfo, ModelRequest, ModelResult
from kb_platform.model_service import ModelService
from kb_platform.plugin_native import PluginError, native_call


class IndependentChatProvider:
    info = ModelInfo('fixture', 'standalone', frozenset({Capability.CHAT}))

    def __init__(self, *, usage=None, text='accepted'):
        self.usage, self.text, self.requests = usage, text, []

    def invoke(self, request):
        self.requests.append(request)
        return ModelResult(self.info.provider, self.info.model, Capability.CHAT,
                           {'text': self.text, 'finish_reason': 'stop'}, self.usage)


class LlmBridgeContracts(unittest.TestCase):
    def setUp(self):
        self.models = ModelService()
        self.addCleanup(self.models.close)

    @staticmethod
    def request(text='question'):
        return ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': text}]})

    def test_public_model_calls_preserve_raw_usage_and_expose_disjoint_rust_usage(self):
        raw = {'prompt_tokens': 13, 'completion_tokens': 7, 'total_tokens': 20,
               'prompt_tokens_details': {'cached_tokens': 5, 'cache_write_tokens': 2}}
        provider = IndependentChatProvider(usage=raw)
        self.models.register('chat', provider)
        result = self.models.invoke('chat', self.request())
        self.assertEqual(result.usage, raw)
        self.assertEqual(result.normalized_usage,
                         {'inputTokens': 6, 'outputTokens': 7, 'totalTokens': 20,
                          'cacheReadTokens': 5, 'cacheWriteTokens': 2})
        self.assertEqual(result.normalized_finish, {'kind': 'stop'})
        self.assertEqual(len(provider.requests), 1)

    def test_missing_or_partial_provider_usage_stays_unknown_without_local_estimation(self):
        provider = IndependentChatProvider()
        self.models.register('chat', provider)
        for usage in (None, {}, {'prompt_tokens': 20}, {'completion_tokens': 7}):
            with self.subTest(usage=usage):
                provider.usage = usage
                result = self.models.invoke('chat', self.request())
                self.assertEqual(result.usage, usage)
                self.assertIsNone(result.normalized_usage)

    def test_large_legacy_text_is_not_copied_into_the_bounded_native_control_message(self):
        text = 'x' * (5 * 1024 * 1024)
        provider = IndependentChatProvider(text=text)
        self.models.register('chat', provider)
        result = self.models.invoke('chat', self.request(text))
        self.assertEqual(provider.requests[0].payload['messages'][0]['content'], text)
        self.assertEqual(result.data['text'], text)
        self.assertEqual(result.normalized_finish, {'kind': 'stop'})

    def test_unavailable_rust_contract_fails_before_the_provider_is_called(self):
        provider = IndependentChatProvider()
        self.models.register('chat', provider)
        with patch('kb_platform.llm_protocol.native_call', side_effect=PluginError('PLUGIN_NATIVE_UNAVAILABLE')):
            with self.assertRaisesRegex(ModelPreflightError, 'PLUGIN_NATIVE_UNAVAILABLE') as caught:
                self.models.invoke('chat', self.request())
        self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(provider.requests, [])

    def test_builtin_luna_structured_completion_uses_real_rust_preflight_and_postflight(self):
        def post(path, payload, headers, purpose, key, *, trace):
            trace['attempt'] += 1
            return {'choices': [{'message': {'content': '{"answer":"accepted"}'}, 'finish_reason': 'stop'}]}

        client = SimpleNamespace(provider_id='fixture', model='luna', retries=0,
                                 base_url='https://fixture.invalid', _headers={}, _post=Mock(side_effect=post),
                                 _cache_get=Mock(return_value=None), _cache_put=Mock(), _record=Mock(),
                                 _save_failed_response=Mock(return_value={}))
        self.models.register('chat', LunaAdapter(client))
        model = self.models.structured('chat')
        with patch('kb_platform.llm_protocol.native_call', wraps=native_call) as bridge:
            result = model.complete(self.request().payload['messages'], role='answer',
                                    schema={'type': 'object'}, call_context={})
        self.assertEqual(result.value, {'answer': 'accepted'})
        self.assertEqual([call.args[0] for call in bridge.call_args_list],
                         ['llm_validate_legacy_chat', 'llm_normalize_legacy_result'])
        self.assertTrue(all(call.kwargs['protocol'] == 'llm-service/1' for call in bridge.call_args_list))
        self.assertIs(result.usage['available'], False)
        self.assertEqual(result.usage['source'], 'unknown')
        self.assertIsNone(result.usage['total_tokens'])
        self.assertFalse(self.models.catalog()['chat'].streaming)
        client._post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
