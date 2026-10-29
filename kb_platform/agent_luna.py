"""Luna adapter; its private transport and OpenAI wire format stop here."""
from .agent_provider import ProviderCompletion


class LunaModelProvider:
    def __init__(self, client):
        self.client = client

    @property
    def manifest(self):
        return getattr(self.client, 'manifest', {})

    @property
    def max_attempts(self):
        return self.client.retries + 1

    def identity(self, messages, output_limit):
        return {'endpoint': self.client.base_url,
                'payload': {'model': self.client.model, 'messages': messages, 'max_tokens': output_limit},
                'transport': getattr(self.client, 'manifest', {}).get('transport')}

    def complete(self, messages, output_limit, *, purpose, key, trace):
        payload = self.identity(messages, output_limit)['payload']
        raw = self.client._post('/chat/completions', payload, self.client._headers, purpose, key, trace=trace)
        choices = raw.get('choices')
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get('message')
        return ProviderCompletion(message.get('content') if isinstance(message, dict) else None,
                                  choice.get('finish_reason'), raw.get('usage'), raw)

    def cache_get(self, key):
        return self.client._cache_get(key)

    def cache_put(self, key, value):
        self.client._cache_put(key, value)

    def record(self, purpose, key, **metadata):
        return self.client._record(purpose, key, **metadata)

    def save_failed_response(self, result, metadata):
        return self.client._save_failed_response(result.diagnostic, metadata)
