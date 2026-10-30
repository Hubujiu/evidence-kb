import json
import math
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


def read_model_config(path):
    from jsonschema import Draft202012Validator
    from .model_http import validate_model_endpoint, validate_provider_options
    from .plugin_configuration import config_schema, TEXT, POSITIVE
    from .rag_testing.budget import validate_ledger_configuration

    resources = {'type': 'object', 'minProperties': 1,
                 'propertyNames': TEXT, 'additionalProperties': {'type': 'integer', 'minimum': 0}}
    provider = config_schema({'adapter': {'enum': ['openai-compatible', 'anthropic']},
        'base_url': TEXT, 'api_key_env': {'type': 'string', 'pattern': '^[A-Za-z_][A-Za-z0-9_]*$'},
        'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0}},
        required=('adapter', 'base_url', 'api_key_env'))
    model = config_schema({'provider': TEXT, 'model': TEXT,
        'capabilities': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                         'items': {'enum': [kind.value for kind in Capability]}},
        'reservation': resources, 'dimension': POSITIVE, 'batch_size': POSITIVE},
        required=('provider', 'model', 'capabilities', 'reservation'))
    schema = config_schema({'version': {'type': 'integer', 'const': 1},
        'budget': config_schema({'path': TEXT, 'caps': resources}, required=('path', 'caps')),
        'providers': {'type': 'object', 'minProperties': 1, 'propertyNames': TEXT, 'additionalProperties': provider},
        'models': {'type': 'object', 'minProperties': 1, 'propertyNames': TEXT, 'additionalProperties': model}},
        required=('version', 'budget', 'providers', 'models'))
    config = json.loads(Path(path).read_text('utf-8'))
    if any(Draft202012Validator(schema).iter_errors(config)):
        raise ValueError('INVALID_MODELS_CONFIG')
    budget = config['budget']
    validate_ledger_configuration(Path(budget['path']), budget['caps'])
    classes = {'openai-compatible': 'OpenAICompatibleAdapter', 'anthropic': 'AnthropicAdapter'}
    for value in config['providers'].values():
        validate_model_endpoint(value['base_url'])
        timeout = value.get('timeout_seconds', 120)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('INVALID_MODEL_PROVIDER_CONFIG')
        if not os.environ.get(value['api_key_env'], ''):
            raise ValueError('MODEL_PROVIDER_CREDENTIAL_MISSING')
    for name, row in config['models'].items():
        selected = config['providers'].get(row['provider'])
        if selected is None:
            raise ValueError('MODEL_PROVIDER_UNAVAILABLE')
        info = validate_provider_options(provider=row['provider'], model=row['model'], endpoint=selected['base_url'],
            api_key=os.environ[selected['api_key_env']], capabilities=row['capabilities'],
            reservation=row['reservation'], caps=budget['caps'], timeout=selected.get('timeout_seconds', 120),
            preferred_batch_size=row.get('batch_size', 1), adapter_name=classes[selected['adapter']])
        required_capability = {'generation': Capability.CHAT, 'embedding': Capability.EMBEDDING,
                               'ocr': Capability.OCR}.get(name)
        if required_capability is not None and required_capability not in info.capabilities:
            raise ValueError('MODEL_ROUTE_CAPABILITY_MISMATCH')
        if name == 'embedding' and ('dimension' not in row or type(row['dimension']) is not int):
            raise ValueError('MODEL_EMBEDDING_DIMENSION_REQUIRED')
        if any(value > budget['caps'][key] for key, value in row['reservation'].items()):
            raise ValueError('MODEL_RESERVATION_EXCEEDS_CAP')
    return config


def from_config(path):
    from .model_http import AnthropicAdapter, OpenAICompatibleAdapter
    from .rag_testing.budget import BudgetLedger

    config = read_model_config(path)
    budget = config['budget']
    ledger = BudgetLedger(Path(budget['path']), budget['caps'])
    service = ModelService()
    try:
        for name, row in config['models'].items():
            provider = config['providers'][row['provider']]
            adapters = {'openai-compatible': OpenAICompatibleAdapter, 'anthropic': AnthropicAdapter}
            adapter = adapters[provider['adapter']](provider=row['provider'], model=row['model'],
                endpoint=provider['base_url'], api_key=os.environ[provider['api_key_env']],
                capabilities=row['capabilities'], ledger=ledger, reservation=row['reservation'],
                timeout=provider.get('timeout_seconds', 120), preferred_batch_size=row.get('batch_size', 1))
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
        self.manifest = deepcopy(manifest) if manifest is not None else {'provider': info.provider, 'model': info.model,
            'dimension': dimension, 'adapter': 'model-service/1', 'route': route, 'revision': info.revision}

    def documents(self, texts, *, request_id=None, audit_context=None, authorize=None):
        if authorize is not None and authorize() is not True:
            raise ValueError('EMBEDDING_AUTHORIZATION_FAILED')
        context = {'purpose': 'embedding_document'}
        if request_id is not None:
            context['request_id'] = request_id
        if audit_context is not None:
            context['audit_context_sha256'] = digest(audit_context)
        result = self._invoke(texts, 'document', context)
        if authorize is not None and authorize() is not True:
            raise ValueError('EMBEDDING_AUTHORIZATION_FAILED')
        return result

    def query(self, query):
        return self._invoke([query], 'query')[0]

    def _invoke(self, texts, task, context=None):
        try:
            result = self._bound.invoke(ModelRequest(Capability.EMBEDDING,
                {'texts': texts, 'task': task, 'dimensions': self.dimension},
                context or {'purpose': 'embedding_query'}))
        except (RemoteModelError, ValueError) as exc:
            raise EmbeddingCallError(exc) from exc
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
