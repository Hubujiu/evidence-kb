from .model_errors import ModelPreflightError, RemoteModelError
from .plugin_native import PluginError, native_call


LLM_PROTOCOL = 'llm-service/1'

_MODEL_ERRORS = {
    'INVALID_LLM_PROVIDER': 'INVALID_MODEL_REGISTRATION',
    'INVALID_LLM_REGISTRATION_ID': 'INVALID_MODEL_REGISTRATION_ID',
    'LLM_PROVIDER_ALREADY_REGISTERED': 'MODEL_REGISTRATION_UNAVAILABLE',
    'LLM_REGISTRATION_ID_REUSED': 'MODEL_REGISTRATION_ID_REUSED',
    'LLM_PROVIDER_UNAVAILABLE': 'MODEL_ROUTE_UNAVAILABLE',
    'LLM_BINDING_CHANGED': 'MODEL_BINDING_CHANGED',
    'LLM_PREPARED_UNAVAILABLE': 'MODEL_BINDING_CHANGED',
    'LLM_CAPABILITY_UNAVAILABLE': 'MODEL_CAPABILITY_UNAVAILABLE',
    'LLM_BUSY': 'MODEL_BUSY',
    'LLM_CLEANUP_BUSY': 'MODEL_CLEANUP_BUSY',
    'INVALID_LLM_RESPONSE': 'INVALID_MODEL_RESULT_IDENTITY',
}


class NativeModelRegistry:
    def __init__(self):
        self.handle = native_call('llm_service_create', protocol=LLM_PROTOCOL)['service']

    def call(self, operation, *, lookup=False, sent=False, **fields):
        if self.handle is None:
            if operation == 'service_inspect':
                return {'routes': [], 'prepared': 0, 'calls': 0}
            raise ValueError('MODEL_REGISTRATION_UNAVAILABLE')
        try:
            result = native_call('llm_' + operation, protocol=LLM_PROTOCOL, service=self.handle, **fields)
        except PluginError as cause:
            code = _MODEL_ERRORS.get(cause.code, cause.code)
            if sent:
                error = RemoteModelError(code)
            elif lookup:
                error = (RemoteModelError(code) if code.startswith('MODEL_') else ModelPreflightError(code))
            else:
                error = ValueError(code)
            if sent or lookup:
                error.model_sent = sent
            raise error from cause
        if operation == 'service_finish_close':
            self.handle = None
        return result


def chat_metadata(provider, model, messages, *, max_tokens=None, options=None):
    return {'provider': provider, 'model': model,
            'messages': [{'role': item.get('role', ''), 'contentIsText': isinstance(item.get('content'), str),
                          'fields': list(item)} for item in messages], 'maxTokens': max_tokens,
            'options': list(options) if options is not None else ['messages', 'max_tokens']}



class CompletionBridge:
    def __init__(self, port, invoke, ownership):
        self._port, self._invoke, self._ownership = port, invoke, ownership

    def __getattr__(self, name):
        return getattr(self._port, name)

    def unwrap(self):
        return self._port

    def complete(self, messages, output_limit, *, purpose, key, trace):
        return self._invoke(self._port, messages, output_limit, purpose=purpose, key=key, trace=trace)

    def identity(self, messages, output_limit):
        with self._ownership():
            return self._port.identity(messages, output_limit)

    def cache_get(self, key):
        with self._ownership():
            return self._port.cache_get(key)

    def cache_put(self, key, value):
        with self._ownership():
            return self._port.cache_put(key, value)

    def record(self, purpose, key, **metadata):
        with self._ownership():
            return self._port.record(purpose, key, **metadata)

    def save_failed_response(self, result, metadata):
        with self._ownership():
            return self._port.save_failed_response(result, metadata)


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
