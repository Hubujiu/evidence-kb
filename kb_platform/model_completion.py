from .agent_provider import ModelProvider, ProviderCompletion
from .protocol_validation import valid_callable
from .model_sdk import Capability, MODEL_PROTOCOL, ModelRequest


def validate_completion_port(port):
    if type(getattr(port, 'max_attempts', None)) is not int or port.max_attempts < 1:
        raise ValueError('INVALID_MODEL_COMPLETION_PORT')
    calls = {
        'identity': (([], 1), {}),
        'complete': (([], 1), {'purpose': '', 'key': '', 'trace': {}}),
        'cache_get': (('',), {}),
        'cache_put': (('', {}), {}),
        'record': (('', ''), {'units': 0, 'cached': False, 'error': '', 'metadata': {}}),
        'save_failed_response': ((None, {}), {}),
    }
    for name, (args, kwargs) in calls.items():
        method = getattr(port, name, None)
        if (not valid_callable(method, *args, **kwargs)
                or getattr(method, '__func__', method) is getattr(ModelProvider, name)):
            raise ValueError('INVALID_MODEL_COMPLETION_PORT')
    return port


class CompletionPort:

    max_attempts = 1

    def __init__(self, adapter, *, invoke=None):
        self.adapter = adapter
        self._invoke = invoke if invoke is not None else adapter.invoke

    def identity(self, messages, output_limit):
        return {'protocol': MODEL_PROTOCOL, 'provider': self.adapter.info.provider,
                'model': self.adapter.info.model, 'endpoint': getattr(self.adapter, 'endpoint', None),
                'revision': self.adapter.info.revision,
                'messages': messages, 'output_limit': output_limit}

    def complete(self, messages, output_limit, *, purpose, key, trace):
        request = ModelRequest(Capability.CHAT, {'messages': messages, 'max_tokens': output_limit},
                               {**trace, 'purpose': purpose})
        try:
            result = self._invoke(request)
        except BaseException as exc:
            trace['attempt'] += int(getattr(exc, 'model_sent', None) is not False)
            raise
        trace['attempt'] += 1
        return ProviderCompletion(result.data['text'], result.data.get('finish_reason'), result.usage)

    def cache_get(self, key):
        return None

    def cache_put(self, key, value):
        pass

    def record(self, purpose, key, **metadata):
        pass

    def save_failed_response(self, result, metadata):
        return {'response_persisted': False}
