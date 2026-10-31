import json
import os
from copy import deepcopy
from pathlib import Path

from .context_budget import digest
from .embeddings import EmbeddingCallError
from .model_errors import RemoteModelError
from .model_service import Capability, Media, ModelRequest, ModelService


def from_clients(chat, embedding=None):
    from .model_adapters import EmbeddingAdapter, LunaAdapter

    service = ModelService()
    service.register('generation', LunaAdapter(chat))
    if embedding is not None:
        service.register('embedding', EmbeddingAdapter(embedding))
    return service


def from_config(path):

    from .model_http import AnthropicAdapter, OpenAICompatibleAdapter
    from .rag_testing.budget import BudgetLedger

    config = json.loads(Path(path).read_text('utf-8'))
    if config.get('version') != 1 or not isinstance(config.get('models'), dict):
        raise ValueError('INVALID_MODELS_CONFIG')
    budget = config['budget']
    ledger = BudgetLedger(Path(budget['path']), budget['caps'])
    service = ModelService()
    try:
        for name, row in config['models'].items():
            provider = config['providers'][row['provider']]
            adapters = {'openai-compatible': OpenAICompatibleAdapter, 'anthropic': AnthropicAdapter}
            if provider['adapter'] not in adapters:
                raise ValueError('UNKNOWN_MODEL_ADAPTER')
            secret = os.environ.get(provider['api_key_env'], '')
            adapter = adapters[provider['adapter']](provider=row['provider'], model=row['model'],
                endpoint=provider['base_url'], api_key=secret, capabilities=row['capabilities'],
                ledger=ledger, reservation=row['reservation'], timeout=provider.get('timeout_seconds', 120),
                preferred_batch_size=row.get('batch_size', 1))
            try:
                service.register(name, adapter, owned=True)
            except BaseException:
                adapter.close()
                raise
        return service
    except BaseException:
        service.close()
        raise


class EmbeddingView:

    def __init__(self, models, route='embedding', *, dimension, manifest=None):
        self.models, self.route, self.dimension = models, route, dimension
        self._bound = models.bind(route, Capability.EMBEDDING)
        info = self._bound.info
        if Capability.EMBEDDING not in info.capabilities or type(dimension) is not int or dimension < 1:
            raise ValueError('INVALID_EMBEDDING_ROUTE')
        self.provider_id, self.model = info.provider, info.model
        self.max_batch_size = info.preferred_batch_size
        if manifest is None:
            with models.lease(route, Capability.EMBEDDING, expected=self._bound.registration_id) as adapter:
                manifest = getattr(adapter, 'embedding_manifest', None)
        self.manifest = deepcopy(manifest) if manifest is not None else {'provider': info.provider, 'model': info.model,
            'dimension': dimension, 'adapter': 'model-service/1', 'route': route, 'revision': info.revision}

    def documents(self, texts, *, request_id=None, audit_context=None, authorize=None):
        return self._invoke(texts, 'document', self._context('document', request_id, audit_context, authorize))

    def query(self, query, *, request_id=None, audit_context=None, authorize=None):
        return self._invoke([query], 'query', self._context('query', request_id, audit_context, authorize))[0]

    @staticmethod
    def _context(task, request_id, audit_context, authorize):
        context = {'purpose': 'embedding_' + task, 'authorize': authorize}
        if request_id is not None:
            context['request_id'] = request_id
        if audit_context is not None:
            context['audit_context'] = deepcopy(audit_context)
            context['audit_context_sha256'] = digest(audit_context)
        return context

    def recover_embedding(self, request_id):
        try:
            with self.models.lease(self.route, Capability.EMBEDDING, expected=self._bound.registration_id) as adapter:
                recover = getattr(adapter, 'recover_embedding', None)
                return recover(request_id) if recover is not None else None
        except (RemoteModelError, ValueError) as exc:
            raise EmbeddingCallError(exc) from exc

    def embedding_preflight(self, request_id, error):
        try:
            with self.models.lease(self.route, Capability.EMBEDDING, expected=self._bound.registration_id) as adapter:
                preflight = getattr(adapter, 'embedding_preflight', None)
                return preflight(request_id, error) if preflight is not None else getattr(error, 'preflight_code', None)
        except (RemoteModelError, ValueError):
            return None

    def _invoke(self, texts, task, context=None):
        authorize = (context or {}).get('authorize')
        if authorize is not None and authorize() is not True:
            raise ValueError('EMBEDDING_AUTHORIZATION_FAILED')
        try:
            result = self._bound.invoke(ModelRequest(Capability.EMBEDDING,
                {'texts': texts, 'task': task, 'dimensions': self.dimension},
                context or {'purpose': 'embedding_query'}))
        except (RemoteModelError, ValueError) as exc:
            raise EmbeddingCallError(exc) from exc
        if authorize is not None and authorize() is not True:
            raise ValueError('EMBEDDING_AUTHORIZATION_FAILED')
        vectors = result.data['vectors']
        if len(vectors) != len(texts) or any(len(v) != self.dimension for v in vectors):
            raise ValueError('EMBEDDING_DIMENSION_MISMATCH')
        return vectors


class OCRView:
    supports_contract_validation = True

    def __init__(self, models, route='generation', *, manifest=None, stats=None):
        self.models, self.route = models, route
        self.maximum_format_corrections = 1 if stats is not None else 0
        self._bound = models.bind(route, Capability.OCR)
        info = self._bound.info
        if Capability.OCR not in info.capabilities:
            raise ValueError('INVALID_OCR_ROUTE')
        self.provider_id, self.model = info.provider, info.model
        self.manifest = deepcopy(manifest) if manifest is not None else {'provider': info.provider, 'model': info.model,
                                    'adapter': 'model-service/1', 'route': route, 'revision': info.revision}
        if stats is not None:
            self.snapshot_stats = stats

    def complete_json(self, system, user, *, purpose, images, validator=None, contract_version=None):
        if purpose != 'ocr' or len(images) != 1:
            raise ValueError('INVALID_OCR_REQUEST')
        data = images[0]
        mime = 'image/png' if data.startswith(b'\x89PNG') else 'image/jpeg'
        return self._bound.invoke(ModelRequest(Capability.OCR,
            {'media': Media(data, mime), 'system': system, 'instruction': user,
             'validator': validator, 'contract_version': contract_version}, {'purpose': 'ocr'})).data


class TextView:

    def __init__(self, models, structured, route='generation'):
        info = models.catalog()[route]
        self.provider_id, self.model = info.provider, info.model
        self.manifest = {'provider': info.provider, 'model': info.model, 'adapter': 'model-service/1'}
        self.structured = structured

    def complete_json(self, system, user, *, purpose):
        return self.structured.complete([{'role': 'system', 'content': system},
            {'role': 'user', 'content': user}], role=purpose, schema={'type': 'object'}, call_context={}).value
