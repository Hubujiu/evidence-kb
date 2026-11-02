from copy import deepcopy

from .embeddings import EmbeddingCallError
from .llm_contract import LlmClient, error_details, model_error
from .model_errors import ModelPreflightError, RemoteModelError, TerminalContractError
from .model_sdk import Capability, Media, ModelRequest
from .ocr_contract import OCR_CONTRACT_VERSION, validate_ocr_response
from .plugin_contracts import ContractError


class KnowledgeModelPolicy:
    def __init__(self, access):
        self._access = access

    def admit(self, request):
        if request['capability'] != Capability.EMBEDDING:
            return {'allowed': True}
        context = request['context']
        if 'audit_context' not in context:
            return {'allowed': False}
        purpose = context.get('purpose')
        if purpose not in {'embedding_query', 'embedding_document'}:
            return {'allowed': False}
        result = self._access.validate_embedding({'audit_context': context['audit_context'],
            'task': purpose.removeprefix('embedding_'), 'provider_id': request['provider']})
        return {'allowed': result['authorized'] is True}

    def validate(self, request):
        if request['contract'] != OCR_CONTRACT_VERSION:
            raise ContractError('MODEL_VALIDATION_CONTRACT_UNAVAILABLE', 'Unknown model validation contract',
                                status_code=422)
        try:
            validate_ocr_response(request['value'])
        except ValueError as error:
            raise ContractError(str(error), str(error), status_code=422,
                                details={'terminal': isinstance(error, TerminalContractError)}) from error
        return {}


class KnowledgeEmbedding:
    def __init__(self, models, route):
        self._bound = models.bind(route, Capability.EMBEDDING)
        metadata, info = self._bound.metadata, self._bound.info
        self.dimension = metadata.get('dimension')
        if type(self.dimension) is not int or self.dimension < 1:
            raise ValueError('MODEL_EMBEDDING_DIMENSION_REQUIRED')
        self.provider_id, self.model = info.provider, info.model
        self.max_batch_size = info.preferred_batch_size
        self.manifest = deepcopy(metadata.get('embedding_manifest')) or {
            'provider': info.provider, 'model': info.model, 'dimension': self.dimension,
            'adapter': 'llm.service/1', 'route': route, 'revision': info.revision}

    def _invoke(self, texts, task, request_id, audit_context, authorize):
        from .context_budget import digest

        context = {'purpose': 'embedding_' + task}
        if request_id is not None:
            context['request_id'] = request_id
        if audit_context is not None:
            context.update(audit_context=deepcopy(audit_context), audit_context_sha256=digest(audit_context))
        if authorize is not None and authorize() is not True:
            raise ModelPreflightError('EMBEDDING_AUTHORIZATION_FAILED')
        try:
            result = self._bound.invoke(ModelRequest(Capability.EMBEDDING,
                {'texts': texts, 'task': task, 'dimensions': self.dimension}, context))
        except (RemoteModelError, ValueError) as error:
            raise EmbeddingCallError(error) from error
        try:
            if authorize is not None and authorize() is not True:
                raise ValueError('EMBEDDING_AUTHORIZATION_FAILED')
        except Exception as error:
            converted = model_error(error_details(error, result=result))
            raise EmbeddingCallError(converted) from error
        return result.data['vectors']

    def documents(self, texts, *, request_id=None, audit_context=None, authorize=None):
        return self._invoke(texts, 'document', request_id, audit_context, authorize)

    def query(self, query, *, request_id=None, audit_context=None, authorize=None):
        return self._invoke([query], 'query', request_id, audit_context, authorize)[0]

    def recover_embedding(self, request_id):
        try:
            return self._bound.call('recover', request_id=request_id)['value']
        except (RemoteModelError, ValueError) as error:
            raise EmbeddingCallError(error) from error

    def embedding_preflight(self, request_id, error):
        try:
            return self._bound.call('preflight', request_id=request_id, error=error_details(error))['value']
        except (RemoteModelError, ValueError):
            return None


class KnowledgeOCR:
    supports_contract_validation = True

    def __init__(self, models, route):
        self._bound = models.bind(route, Capability.OCR)
        info, metadata = self._bound.info, self._bound.metadata
        self.provider_id, self.model = info.provider, info.model
        self.manifest = deepcopy(metadata.get('manifest')) or {
            'provider': info.provider, 'model': info.model, 'adapter': 'llm.service/1', 'revision': info.revision}
        self.maximum_format_corrections = metadata.get('maximum_format_corrections', 0)
        if self._bound.statistics:
            self.snapshot_stats = lambda: self._bound.call('statistics')['value']

    def complete_json(self, system, user, *, purpose, images, validator=None, contract_version=None):
        if purpose != 'ocr' or len(images) != 1 or contract_version != OCR_CONTRACT_VERSION:
            raise ModelPreflightError('INVALID_OCR_REQUEST')
        if validator is not None and validator is not validate_ocr_response:
            raise ModelPreflightError('MODEL_CALLBACK_NOT_PORTABLE')
        data = images[0]
        mime = 'image/png' if data.startswith(b'\x89PNG') else 'image/jpeg'
        result = self._bound.invoke(ModelRequest(Capability.OCR, {'media': Media(data, mime),
            'system': system, 'instruction': user, 'contract_version': contract_version},
            {'purpose': purpose}), validation=contract_version).data
        validate_ocr_response(result)
        return result


def knowledge_model(dependency, config, access):
    from .agent_model import BudgetLedger
    from .context_budget import ContextBudget
    from .knowledge_model_ports import AuxiliaryModelEndpoint

    registry = LlmClient(dependency)
    routes = {'generation': 'generation', 'embedding': 'embedding', 'ocr': 'ocr', **config.get('routes', {})}
    selected = registry.structured(routes['generation'], policy=ContextBudget(**config.get('policy', {})),
                                   ledger=BudgetLedger(**config.get('budget', {})))
    catalog = registry.catalog()
    embedding = KnowledgeEmbedding(registry, routes['embedding']) if routes['embedding'] in catalog else None
    ocr_route = routes['ocr'] if routes['ocr'] in catalog else routes['generation']
    ocr = KnowledgeOCR(registry, ocr_route) if Capability.OCR in catalog[ocr_route].capabilities else None
    selected.capability_endpoint = AuxiliaryModelEndpoint(embedding=embedding, ocr=ocr, authorization=access)
    return selected
