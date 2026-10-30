from uuid import uuid4

from .agent_luna import LunaModelProvider
from .agent_provider import ProviderCompletion
from .context_budget import digest
from .model_service import Capability, ModelInfo, ModelResult


class LunaAdapter:
    def __init__(self, client):
        self.client = client
        provider, model = getattr(client, 'provider_id', None), getattr(client, 'model', None)
        self.info = ModelInfo(provider if isinstance(provider, str) else 'magpie_luna',
                              model if isinstance(model, str) else 'configured',
                              frozenset({Capability.CHAT, Capability.OCR}), frozenset({'text', 'image'}))

    def completion_port(self):
        return LunaModelProvider(self.client)

    def invoke(self, request):
        body = request.payload
        if request.capability == Capability.OCR:
            data = self.client.complete_json(body.get('system', 'Transcribe the image as JSON.'),
                body.get('instruction', 'Transcribe all visible text.'), purpose='ocr',
                images=[body['media'].data], validator=body.get('validator'),
                contract_version=body.get('contract_version'))
            return ModelResult(self.info.provider, self.info.model, request.capability, data)
        port = self.completion_port()
        limit = body.get('max_tokens', 1500)
        identity = port.identity(body['messages'], limit)
        trace = {**request.call_context, 'request_id': request.call_context.get('call_id') or uuid4().hex,
                 'attempt': 0}
        response = port.complete(body['messages'], limit, purpose='model_chat', key=digest(identity), trace=trace)
        return ModelResult(self.info.provider, self.info.model, request.capability,
                           {'text': response.content, 'finish_reason': response.finish_reason},
                           response.usage, trace['request_id'])


class EmbeddingAdapter:
    def __init__(self, provider):
        self.provider = provider
        batch_size = getattr(provider, 'max_batch_size', 1)
        self.info = ModelInfo(getattr(provider, 'provider_id', 'embedding'),
                              getattr(provider, 'model', 'configured'), frozenset({Capability.EMBEDDING}),
                              preferred_batch_size=batch_size)

    def invoke(self, request):
        dimension = getattr(self.provider, 'dimension', None)
        if dimension is not None and request.payload.get('dimensions', dimension) != dimension:
            raise ValueError('EMBEDDING_DIMENSION_MISMATCH')
        texts = request.payload['texts']
        vectors = ([self.provider.query(text) for text in texts]
                   if request.payload.get('task') == 'query' else self.provider.documents(texts))
        return ModelResult(self.info.provider, self.info.model, request.capability, {'vectors': vectors})


class CompletionPort:

    max_attempts = 1

    def __init__(self, adapter):
        self.adapter = adapter

    def identity(self, messages, output_limit):
        return {'protocol': 'model-service/1', 'provider': self.adapter.info.provider,
                'model': self.adapter.info.model, 'endpoint': self.adapter.endpoint,
                'revision': self.adapter.info.revision,
                'messages': messages, 'output_limit': output_limit}

    def complete(self, messages, output_limit, *, purpose, key, trace):
        from .model_service import ModelRequest

        request = ModelRequest(Capability.CHAT, {'messages': messages, 'max_tokens': output_limit},
                               {**trace, 'purpose': purpose})
        try:
            result = self.adapter.invoke(request)
        except BaseException as exc:
            trace['attempt'] += int(getattr(exc, 'model_sent', False))
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
