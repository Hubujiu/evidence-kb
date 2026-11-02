from .model_errors import ModelPreflightError, RemoteModelError
from .plugin_native import PluginError, native_call


LLM_PROTOCOL = 'llm-service/1'


def _native(command, *, sent=False, **fields):
    try:
        return native_call(command, protocol=LLM_PROTOCOL, **fields)
    except PluginError as cause:
        error = RemoteModelError(cause.code) if sent else ModelPreflightError(cause.code)
        error.model_sent = sent
        raise error from cause


def validate_text_request(provider, model, messages, *, max_tokens=None, options=None):
    return _native('llm_validate_legacy_chat', provider=provider, model=model,
        messages=[{'role': item.get('role', ''), 'contentIsText': isinstance(item.get('content'), str),
                   'fields': list(item)} for item in messages], maxTokens=max_tokens,
        options=list(options) if options is not None else ['messages', 'max_tokens'])


def normalize_text_result(provider, model, content, finish_reason, usage, *,
                          expected_provider=None, expected_model=None):
    return _native('llm_normalize_legacy_result', sent=True, provider=provider, model=model,
        expectedProvider=provider if expected_provider is None else expected_provider,
        expectedModel=model if expected_model is None else expected_model,
        textIsText=isinstance(content, str), finishReason=finish_reason, usage=usage)


class CompletionBridge:
    def __init__(self, port, info):
        self._port, self._info = port, info

    def __getattr__(self, name):
        return getattr(self._port, name)

    def unwrap(self):
        return self._port

    def complete(self, messages, output_limit, *, purpose, key, trace):
        validate_text_request(self._info.provider, self._info.model, messages, max_tokens=output_limit)
        result = self._port.complete(messages, output_limit, purpose=purpose, key=key, trace=trace)
        if isinstance(result.content, str):
            normalize_text_result(self._info.provider, self._info.model, result.content,
                                  result.finish_reason, result.usage)
        return result


class StreamAssembler:
    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.state, self.response = None, None

    def push(self, event):
        result = native_call('llm_stream_apply', state=self.state, event=event,
                             provider=self.provider, model=self.model, protocol=LLM_PROTOCOL)
        self.state, self.response = result['state'], result['response']
        return self.response

    def end(self):
        return self.push(None)
