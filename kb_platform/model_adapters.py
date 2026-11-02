from copy import deepcopy
from typing import Protocol
from uuid import uuid4

from .agent_luna import LunaModelProvider
from .context_budget import digest
from .model_sdk import Capability, ModelInfo, ModelResult
from .model_errors import ModelPreflightError


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
            if 'max_tokens' in body:
                raise ModelPreflightError('UNSUPPORTED_MODEL_OPTION')
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
                           response.usage, trace['request_id'],
                           physical_request_id=trace.get('last_physical_request_id'))


class ObservedEmbeddingPort(Protocol):
    manifest: dict
    def documents(self, texts, *, request_id, audit_context, authorize): ...
    def query(self, text, *, request_id, audit_context, authorize): ...
    def recover_embedding(self, request_id): ...
    def embedding_preflight(self, request_id, error): ...


class EmbeddingAdapter:
    def __init__(self, provider):
        self.provider = provider
        factory = getattr(provider, 'embedding_port', None)
        self._observed: ObservedEmbeddingPort | None = factory() if factory is not None else None
        self.embedding_manifest = deepcopy(self._observed.manifest) if self._observed is not None else None
        batch_size = getattr(provider, 'max_batch_size', 1)
        self.info = ModelInfo(getattr(provider, 'provider_id', 'embedding'),
                              getattr(provider, 'model', 'configured'), frozenset({Capability.EMBEDDING}),
                              preferred_batch_size=batch_size)

    def invoke(self, request):
        dimension = getattr(self.provider, 'dimension', None)
        if dimension is not None and request.payload.get('dimensions', dimension) != dimension:
            raise ValueError('EMBEDDING_DIMENSION_MISMATCH')
        texts = request.payload['texts']
        if self._observed is not None:
            options = {key: request.call_context.get(key) for key in ('request_id', 'audit_context', 'authorize')}
            if request.payload.get('task') == 'query':
                if len(texts) != 1:
                    raise ValueError('OBSERVED_EMBEDDING_QUERY_REQUIRES_ONE_INPUT')
                vectors = [self._observed.query(texts[0], **options)]
            else:
                vectors = self._observed.documents(texts, **options)
        else:
            vectors = ([self.provider.query(text) for text in texts]
                       if request.payload.get('task') == 'query' else self.provider.documents(texts))
        return ModelResult(self.info.provider, self.info.model, request.capability, {'vectors': vectors})

    def recover_embedding(self, request_id):
        return self._observed.recover_embedding(request_id) if self._observed is not None else None

    def embedding_preflight(self, request_id, error):
        return (self._observed.embedding_preflight(request_id, error) if self._observed is not None
                else getattr(error, 'preflight_code', None))
