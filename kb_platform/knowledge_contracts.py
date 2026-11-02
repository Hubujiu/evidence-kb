from copy import copy
from dataclasses import asdict, dataclass
from inspect import Parameter, Signature
import json
from threading import Lock
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from .agent_model import CompletionEnvelope
from .agent_context import ContextPolicy
from .plugin_contracts import ContractError, Method
from .plugin_configuration import CONTEXT_PROPERTIES
from .session_identity import Actor, PlatformError


TEXT = {'type': 'string', 'minLength': 1}
OBJECT = {'type': 'object'}
ARRAY = {'type': 'array'}
INTEGER = {'type': 'integer', 'minimum': 0}
NULL_OBJECT = {'type': ['object', 'null']}
ACTOR = {'type': 'object', 'properties': {'tenant_id': TEXT, 'principal_id': TEXT},
         'required': ['tenant_id', 'principal_id'], 'additionalProperties': False}
EMPTY = {'type': 'object', 'additionalProperties': False}


def object_schema(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


POLICY_RESPONSE = object_schema({**CONTEXT_PROPERTIES,
    'evidence_strategy': {'enum': ['prefix', 'query_blocks', 'coverage_blocks']}}, list(CONTEXT_PROPERTIES))
MODEL_METADATA_RESPONSE = object_schema({
    'policy': POLICY_RESPONSE, 'capabilities': OBJECT, 'provider_id': TEXT,
    'manifest': OBJECT, 'max_attempts': {'type': 'integer', 'minimum': 1}})
PREPARED_RESPONSE = object_schema({
    'messages': {'type': 'array', 'minItems': 1, 'items': object_schema({
        'role': {'enum': ['system', 'user', 'assistant', 'tool']}, 'content': {'type': 'string'}})},
    'budget': {'type': 'object', 'properties': {
        'input_limit': {'type': 'integer', 'minimum': 1},
        'output_reserve': CONTEXT_PROPERTIES['output_reserve'],
        'model_window': CONTEXT_PROPERTIES['model_window'],
        'safety_reserve': CONTEXT_PROPERTIES['safety_reserve'],
        'strict_tokens': {'type': 'boolean'}, 'window_capability_verified': {'type': 'boolean'},
        'prose_line_safe_fallback_used': {'type': 'boolean'}},
        'required': ['input_limit'], 'additionalProperties': True},
    'estimate': {'type': 'object', 'properties': {
        'tokens': INTEGER, 'kind': TEXT, 'exact': {'type': 'boolean'},
        'capability_verified': {'type': 'boolean'}, 'utf8_bytes': INTEGER, 'note': {'type': 'string'}},
        'required': ['tokens', 'kind'], 'additionalProperties': True},
    'compaction_needed': {'type': 'boolean'},
    'projection': {'type': 'object', 'properties': {
        'role': TEXT, 'state': OBJECT, 'question': {'type': 'string'},
        'history': {'type': 'array', 'items': OBJECT},
        'evidence': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'citation_id': TEXT, 'text': {'type': 'string'}, 'ref': TEXT},
            'required': ['citation_id'], 'additionalProperties': True}},
        'checkpoint': NULL_OBJECT, 'omitted_event_refs': {'type': 'array', 'items': OBJECT}},
        'required': ['role', 'state', 'question', 'history', 'evidence', 'omitted_event_refs'],
        'additionalProperties': True},
})
CONTEXT_RESPONSE = object_schema({'prepared': PREPARED_RESPONSE,
    'evidence': {'type': 'array', 'items': {'type': 'object',
        'properties': {'citation_id': TEXT}, 'required': ['citation_id'], 'additionalProperties': True}}})


@dataclass(frozen=True)
class Operation:
    properties: dict
    required: tuple
    response: dict

    def method(self):
        return Method(object_schema(self.properties, list(self.required)), self.response)


def operation(names, *, optional=None, response=OBJECT):
    defaults = {'actor': ACTOR, 'state': OBJECT, 'response': OBJECT, 'payload': OBJECT,
                'body': OBJECT, 'policy': NULL_OBJECT, 'source_policy': OBJECT,
                'config': NULL_OBJECT, 'constraints': {'type': ['array', 'null'], 'items': TEXT},
                'workflow_state': NULL_OBJECT,
                'evidence': ARRAY, 'citation_ids': ARRAY, 'exclude_citation_ids': ARRAY,
                'readers': ARRAY, 'filters': {'type': ['array', 'null'], 'items': OBJECT},
                'column': {'type': ['string', 'null']}, 'current_step': {'type': ['string', 'null']},
                'facts': NULL_OBJECT,
                'assistant_event': NULL_OBJECT, 'expected_revision': INTEGER,
                'offset': INTEGER, 'limit': INTEGER, 'lease_seconds': INTEGER,
                'hops': INTEGER, 'document_limit': INTEGER, 'fragment_limit': INTEGER,
                'max_rows': INTEGER}
    fields = names.split()
    extras = (optional or '').split()
    properties = {name: defaults.get(name, TEXT) for name in fields + extras}
    return Operation(properties, tuple(fields), response)


SESSION_OPERATIONS = {
    'create': operation('actor scope_id source_policy', optional='config_name'),
    'load': operation('actor session_id'),
    'list_sessions': operation('actor', optional='limit offset', response=ARRAY),
    'events': operation('actor session_id', response=ARRAY),
    'begin': operation('actor session_id request_id expected_revision fingerprint', optional='lease_seconds'),
    'append': operation('actor session_id token kind payload group_id', optional='agent', response=INTEGER),
    'artifact': operation('actor session_id token body'),
    'read_artifact': operation('actor session_id artifact_id'),
    'finish': operation('actor session_id token state response', optional='assistant_event'),
    'fail': operation('actor session_id token state response'),
}
EVIDENCE_OPERATIONS = {
    'authorize': operation('actor scope_id'),
    'sources': operation('actor scope_id', response=ARRAY),
    'read': operation('actor scope_id citation_ids policy', response=ARRAY),
    'revalidate': operation('actor scope_id evidence policy', response=ARRAY),
    'search': operation('actor scope_id query policy', optional='limit exclude_citation_ids granularity', response=ARRAY),
    'search_pages': operation('actor scope_id query policy', optional='limit exclude_citation_ids', response=ARRAY),
    'search_tables': operation('actor scope_id query policy', optional='limit exclude_citation_ids', response=ARRAY),
    'search_pipeline': operation('actor scope_id query policy', optional='config exclude_citation_ids'),
    'pipeline_capabilities': operation('actor scope_id policy'),
    'navigate_references': operation('actor scope_id citation_ids policy', optional='hops document_limit fragment_limit'),
    'table': operation('actor scope_id citation_id policy', optional='operation column filters'),
    'workflow_next': operation('actor scope_id citation_id policy', optional='current_step facts'),
    'table_join': operation('actor scope_id citation_ids policy left_key right_key', optional='how max_rows'),
}
APPLICATION_OPERATIONS = {
    'capabilities': operation(''),
    'create': operation('actor scope_id', optional='policy config_name'),
    'get': operation('actor session_id'),
    'history': operation('actor session_id'),
    'artifact': operation('actor session_id artifact_id'),
    'sources': operation('actor scope_id', response=ARRAY),
    'authority': operation('actor scope_id'),
    'list_sessions': operation('actor', optional='limit offset'),
    'turn': operation('actor session_id message request_id expected_revision',
                      optional='policy constraints workflow_state'),
    'model_status': operation(''),
}


def methods(operations):
    return {name: declaration.method() for name, declaration in operations.items()}


def domain_call(call, request):
    try:
        return call(request)
    except ContractError as error:
        details = error.details
        if details.get('kind') == 'model':
            from .model_errors import RemoteModelError

            converted = RemoteModelError(error.code, details.get('status'),
                                         retry_after_seconds=details.get('retry_after_seconds'))
        else:
            converted = PlatformError(error.code, str(error), error.status_code)
        for name in ('call_record', 'model_sent', 'physical_request_id', 'preflight_code'):
            if details.get(name) is not None:
                setattr(converted, name, details[name])
        converted.details = details
        raise converted from error


class ObjectEndpoint:
    def __init__(self, implementation, operations):
        self._implementation, self._operations = implementation, operations

    def __getattr__(self, name):
        if name.startswith('_') or name not in self._operations:
            raise AttributeError(name)

        def invoke(request):
            values = dict(request)
            if 'actor' in values:
                values['actor'] = Actor(**values['actor'])
            return json.loads(json.dumps(getattr(self._implementation, name)(**values), allow_nan=False))
        return invoke


class ObjectClient:
    def __init__(self, dependency, operations):
        self._dependency, self._operations = dependency, operations

    def __getattr__(self, name):
        if name.startswith('_') or name not in self._operations:
            raise AttributeError(name)
        declaration = self._operations[name]

        def invoke(*args, **kwargs):
            parameters = [Parameter(key, Parameter.POSITIONAL_OR_KEYWORD,
                default=Parameter.empty if key in declaration.required else None)
                for key in declaration.properties]
            bound = Signature(parameters).bind(*args, **kwargs)
            request = dict(bound.arguments)
            if 'actor' in request:
                request['actor'] = asdict(request['actor'])
            if 'exclude_citation_ids' in request:
                request['exclude_citation_ids'] = list(request['exclude_citation_ids'])
            return domain_call(getattr(self._dependency, name), request)
        return invoke


class SessionEndpoint(ObjectEndpoint):
    def __init__(self, store):
        super().__init__(store, SESSION_OPERATIONS)


class SessionClient(ObjectClient):
    def __init__(self, dependency):
        super().__init__(dependency, SESSION_OPERATIONS)


class EvidenceEndpoint(ObjectEndpoint):
    def __init__(self, gateway):
        super().__init__(gateway, EVIDENCE_OPERATIONS)
        self._pending, self._lock = {}, Lock()

    def close(self):
        with self._lock:
            pending, self._pending = self._pending, {}
        for _, _, continuation in pending.values():
            continuation.close()

    def metadata(self, request):
        gateway = self._implementation
        reranker = gateway.pipeline_reranker
        return {'provider_id': gateway.provider_id, 'rerank': reranker is not None,
                'reranker_provider_id': getattr(reranker, 'provider_id', None),
                'reranker_manifest': getattr(reranker, 'manifest', {})}

    def prepare_pipeline(self, request):
        from .retrieval_pipeline import staged

        values = dict(request)
        values['actor'] = Actor(**values['actor'])
        pending = staged(self._implementation, **values)
        try:
            candidates = next(pending)
        except StopIteration as completed:
            return {'result': json.loads(json.dumps(completed.value, allow_nan=False))}
        with self._lock:
            expired = [key for key, (_, deadline, _) in self._pending.items() if deadline < monotonic()]
            for key in expired:
                self._pending.pop(key)[2].close()
            if len(self._pending) >= 64:
                pending.close()
                raise PlatformError('RETRIEVAL_STAGE_CAPACITY', 'Too many pending retrieval stages', 503)
            token = uuid4().hex
            self._pending[token] = (request['actor'], monotonic() + 900, pending)
        return {'token': token, 'candidates': candidates}

    def finish_pipeline(self, request):
        with self._lock:
            entry = self._pending.get(request['token'])
            if entry is None or entry[0] != request['actor']:
                raise PlatformError('RETRIEVAL_STAGE_NOT_FOUND', 'Retrieval continuation is unavailable', 404)
            del self._pending[request['token']]
        _, deadline, pending = entry
        try:
            if deadline < monotonic():
                raise PlatformError('RETRIEVAL_STAGE_EXPIRED', 'Retrieval continuation expired', 409)
            if request.get('cancel', False):
                return {}
            try:
                pending.send(request['ranking'])
            except StopIteration as completed:
                return json.loads(json.dumps(completed.value, allow_nan=False))
            raise PlatformError('INVALID_RETRIEVAL_STAGE', 'Unexpected retrieval continuation', 503)
        finally:
            pending.close()


class RerankTextClient:
    def __init__(self, model, metadata):
        self.structured, self._turn_complete = model, None
        self.provider_id = metadata['reranker_provider_id']
        self.manifest = metadata['reranker_manifest']

    def bind_turn(self, completion):
        bound = copy(self)
        bound._turn_complete = completion
        return bound

    def complete_json(self, system, user, *, purpose):
        if self._turn_complete is not None:
            return self._turn_complete(self.structured, system, user, purpose)
        return self.structured.complete([{'role': 'system', 'content': system},
            {'role': 'user', 'content': user}], role=purpose,
            schema={'type': 'object'}, call_context={}).value


class EvidenceClient(ObjectClient):
    def __init__(self, dependency, model=None):
        super().__init__(dependency, EVIDENCE_OPERATIONS)
        metadata = domain_call(dependency.metadata, {})
        self.provider_id = metadata['provider_id']
        self.pipeline_reranker = None
        if metadata['rerank'] and model is not None:
            from .retrieval_pipeline import LunaCandidateReranker

            self.pipeline_reranker = LunaCandidateReranker(RerankTextClient(model, metadata))

    def search_pipeline(self, actor, scope_id, query, policy, config=None, *, exclude_citation_ids=()):
        request = {'actor': asdict(actor), 'scope_id': scope_id, 'query': query, 'policy': policy,
                   'config': config, 'exclude_citation_ids': list(exclude_citation_ids)}
        prepared = domain_call(self._dependency.prepare_pipeline, request)
        if 'result' in prepared:
            return prepared['result']
        continuation = {'actor': asdict(actor), 'token': prepared['token']}
        try:
            if self.pipeline_reranker is None:
                raise PlatformError('RERANKER_NOT_CONFIGURED', 'No reranker is bound to this workflow', 409)
            ranking = self.pipeline_reranker.rank(query, prepared['candidates'])
        except BaseException:
            domain_call(self._dependency.finish_pipeline, {**continuation, 'cancel': True})
            raise
        return domain_call(self._dependency.finish_pipeline, {**continuation, 'ranking': ranking})


EVIDENCE_METHODS = {**methods(EVIDENCE_OPERATIONS),
    'metadata': Method(EMPTY, OBJECT),
    'prepare_pipeline': Method(EVIDENCE_OPERATIONS['search_pipeline'].method().request,
        {'oneOf': [object_schema({'result': OBJECT}),
                   object_schema({'token': TEXT, 'candidates': ARRAY})]}),
    'finish_pipeline': Method({'oneOf': [
        object_schema({'actor': ACTOR, 'token': TEXT, 'ranking': ARRAY}),
        object_schema({'actor': ACTOR, 'token': TEXT, 'cancel': {'const': True}})]}, OBJECT)}


MODEL_METHODS = {
    'metadata': Method(EMPTY, MODEL_METADATA_RESPONSE),
    'complete': Method(object_schema({
        'messages': {'type': 'array', 'minItems': 1, 'items': OBJECT},
        'role': TEXT, 'schema': OBJECT, 'call_context': OBJECT,
        'output_limit': {'type': 'integer', 'minimum': 1}}),
        object_schema({'token': TEXT, 'completion': object_schema({
            'value': OBJECT, 'request_id': TEXT, 'usage': OBJECT, 'attempts': INTEGER,
            'finish_reason': TEXT, 'cache_kind': TEXT, 'request_hash': TEXT})})),
    'budget': Method(EMPTY, OBJECT),
    'turn_budget': Method(object_schema({'session_id': TEXT, 'turn_id': TEXT}), NULL_OBJECT),
    'settle': Method(object_schema({'token': TEXT, 'accept': {'type': 'boolean'}}), EMPTY),
}


class ModelEndpoint:
    def __init__(self, model):
        self._model = model
        self._pending, self._lock = {}, Lock()

    def close(self):
        with self._lock:
            self._pending.clear()

    def metadata(self, request):
        model = self._model
        return {'policy': asdict(model.policy), 'capabilities': model.capabilities,
                'provider_id': getattr(model, 'provider_id', 'unknown'),
                'manifest': getattr(model, 'manifest', {}),
                'max_attempts': getattr(model, 'max_attempts', getattr(model.provider, 'max_attempts', 1))}

    def complete(self, request):
        token = uuid4().hex
        with self._lock:
            expired = [key for key, (deadline, _) in self._pending.items() if deadline < monotonic()]
            for key in expired:
                del self._pending[key]
            if len(self._pending) >= 64:
                raise ContractError('MODEL_VALIDATION_CAPACITY', 'Too many pending model validations', status_code=503)
            self._pending[token] = (monotonic() + 900, None)
        try:
            envelope = self._model.complete(**request, cache_result=False)
            with self._lock:
                self._pending[token] = (monotonic() + 900, envelope)
            return {'completion': envelope.to_dict(), 'token': token}
        except Exception as error:
            with self._lock:
                self._pending.pop(token, None)
            if not hasattr(error, 'code'):
                raise
            details = {'kind': 'model', 'status': getattr(error, 'status', None),
                       'retry_after_seconds': getattr(error, 'retry_after_seconds', None)}
            for field in ('model_sent', 'physical_request_id', 'preflight_code'):
                if getattr(error, field, None) is not None:
                    details[field] = getattr(error, field)
            if hasattr(error, 'call_record'):
                details['call_record'] = error.call_record
            raise ContractError(error.code, str(error), status_code=502, details=details) from error

    def settle(self, request):
        with self._lock:
            pending = self._pending.pop(request['token'], None)
        if pending is None or pending[0] < monotonic() or pending[1] is None:
            raise ContractError('MODEL_VALIDATION_NOT_FOUND', 'Model validation receipt is unavailable', status_code=409)
        if request['accept']:
            try:
                self._model.accept_completion(pending[1])
            except Exception as error:
                record = {key: value for key, value in pending[1].to_dict().items() if key != 'value'}
                raise ContractError('MODEL_CACHE_COMMIT_FAILED', 'Unable to store the validated completion',
                    status_code=503, details={'kind': 'model', 'call_record': record}) from error
        return {}

    def budget(self, request):
        return self._model.ledger.snapshot()

    def turn_budget(self, request):
        return self._model.turn_budget_snapshot(**request)


class ModelClient:
    def __init__(self, dependency):
        self._dependency = dependency
        metadata = domain_call(dependency.metadata, {})
        self.policy = ContextPolicy(**metadata['policy'])
        self.capabilities, self.manifest = metadata['capabilities'], metadata['manifest']
        self.provider_id = metadata['provider_id']
        self.max_attempts = metadata['max_attempts']
        self.provider = SimpleNamespace(max_attempts=self.max_attempts)
        self.ledger = SimpleNamespace(snapshot=lambda: domain_call(dependency.budget, {}))

    def complete(self, messages, *, role, schema, call_context, output_limit=1500, validator=None):
        response = domain_call(self._dependency.complete, {'messages': messages, 'role': role,
            'schema': schema, 'call_context': call_context, 'output_limit': output_limit})
        result = response['completion']
        envelope = CompletionEnvelope(**result)
        known = {key: value for key, value in result.items() if key != 'value'}
        try:
            if validator is not None:
                validator(envelope.value)
        except BaseException as error:
            settlement_error = None
            try:
                domain_call(self._dependency.settle, {'token': response['token'], 'accept': False})
            except Exception as failure:
                settlement_error = {'code': getattr(failure, 'code', type(failure).__name__),
                                    'operation': 'reject', 'retried': False}
            if isinstance(error, ValueError):
                from .model_errors import RemoteModelError

                converted = RemoteModelError('INVALID_AGENT_OUTPUT')
                converted.call_record = dict(known)
                converted.call_record.update(error='INVALID_AGENT_OUTPUT', validator_reason=str(error))
                if settlement_error is not None:
                    converted.call_record['cache_settlement_error'] = settlement_error
                if hasattr(error, 'issue'):
                    converted.call_record['validator_issue'] = error.issue
                raise converted from error
            error.call_record = {**(getattr(error, 'call_record', None) or {}), **known}
            if settlement_error is not None:
                error.call_record['cache_settlement_error'] = settlement_error
            raise
        try:
            domain_call(self._dependency.settle, {'token': response['token'], 'accept': True})
        except Exception as error:
            error.call_record = {**(getattr(error, 'call_record', None) or {}), **known,
                'cache_settlement_error': {'code': getattr(error, 'code', type(error).__name__),
                                           'operation': 'accept', 'retried': False}}
            raise
        return envelope

    def turn_budget_snapshot(self, session_id, turn_id):
        return domain_call(self._dependency.turn_budget, {'session_id': session_id, 'turn_id': turn_id})


class ApplicationEndpoint(ObjectEndpoint):
    def __init__(self, application, model):
        super().__init__(application, APPLICATION_OPERATIONS)
        self._model = model

    def model_status(self, request):
        return {'policy': asdict(self._model.policy), 'budget': self._model.ledger.snapshot()}


class ApplicationClient(ObjectClient):
    def __init__(self, application):
        class Endpoint:
            def __getattr__(self, name):
                if name not in APPLICATION_OPERATIONS:
                    raise AttributeError(name)
                return lambda request: application.run({'operation': name, 'request': request})
        super().__init__(Endpoint(), APPLICATION_OPERATIONS)
        self.host = application.host
        self.composition = application

    @property
    def model(self):
        status = self.model_status()
        return SimpleNamespace(policy=ContextPolicy(**status['policy']),
                               ledger=SimpleNamespace(snapshot=lambda: self.model_status()['budget']))
