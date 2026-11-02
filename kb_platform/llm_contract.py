import base64
from copy import deepcopy
from dataclasses import asdict
import json
import re

from .agent_model import AgentModel
from .agent_provider import ProviderCompletion
from .contract_transfer import JsonClient, JsonEndpoint, TransferError
from .model_errors import ModelPreflightError, RemoteModelError, TerminalContractError
from .model_sdk import Capability, Media, ModelInfo, ModelRequest, ModelResult, validate_request, validate_result
from .plugin_contracts import ContractError, Method, schema_validator


PROTOCOL = 'llm.service/1'
TEXT = {'type': 'string', 'minLength': 1}
OBJECT = {'type': 'object'}


def object_schema(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else list(required), 'additionalProperties': False}


EMPTY = object_schema({})
MODEL_INFO = object_schema({'provider': TEXT, 'model': TEXT,
    'capabilities': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                     'items': {'enum': [kind.value for kind in Capability]}},
    'input_modalities': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                         'items': {'enum': ['text', 'image', 'audio']}},
    'streaming': {'type': 'boolean'}, 'revision': TEXT,
    'preferred_batch_size': {'type': 'integer', 'minimum': 1}, 'protocol': {'const': 'model-service/1'}})
CATALOG_RESPONSE = object_schema({'protocol': {'const': PROTOCOL}, 'routes': {
    'type': 'object', 'propertyNames': TEXT, 'additionalProperties': object_schema({
        'info': MODEL_INFO, 'registration': {}, 'metadata': OBJECT,
        'max_attempts': {'type': 'integer', 'minimum': 1}, 'statistics': {'type': 'boolean'}})}})
METHODS = {'catalog': Method(EMPTY, CATALOG_RESPONSE), 'exchange': Method(OBJECT, OBJECT)}
POLICY_METHODS = {
    'admit': Method(object_schema({'provider': TEXT, 'model': TEXT, 'capability': TEXT, 'context': OBJECT}),
                    object_schema({'allowed': {'type': 'boolean'}})),
    'validate': Method(object_schema({'contract': TEXT, 'value': OBJECT}), EMPTY),
}
BINDING = {'route': TEXT, 'registration': {}}
MESSAGES = {'type': 'array', 'minItems': 1, 'items': object_schema({
    'role': {'enum': ['system', 'user', 'assistant']}, 'content': {'type': 'string'}})}
COMPLETION = {'messages': MESSAGES, 'output_limit': {'type': 'integer', 'minimum': 1}}
OPERATIONS = {
    'identity': COMPLETION,
    'complete': {**COMPLETION, 'purpose': TEXT, 'key': TEXT, 'trace': OBJECT},
    'cache_get': {'key': TEXT},
    'cache_put': {'key': TEXT, 'value': OBJECT},
    'record': {'purpose': TEXT, 'key': TEXT, 'metadata': OBJECT},
    'diagnostic': {'result': OBJECT, 'metadata': OBJECT},
    'invoke': {'capability': {'enum': [kind.value for kind in Capability]}, 'payload': OBJECT,
               'context': OBJECT, 'validation': {'type': ['string', 'null']}},
    'recover': {'request_id': TEXT},
    'preflight': {'request_id': TEXT, 'error': OBJECT},
    'statistics': {},
}
OPERATION_SCHEMAS = {name: object_schema({
    'version': {'const': 1}, 'op': {'const': name}, **BINDING, **properties})
    for name, properties in OPERATIONS.items()}
_VALIDATORS = {name: schema_validator(schema) for name, schema in OPERATION_SCHEMAS.items()}
ERROR_FIELDS = ('status', 'retry_after_seconds', 'model_sent', 'physical_request_id',
                'request_id', 'call_record', 'preflight_code', 'output_diagnostics', 'observed_usage')
_UNOBSERVED = object()


def _registration(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _encode(value):
    if isinstance(value, Media):
        return {'$media': {'mime_type': value.mime_type,
                           'base64': base64.b64encode(value.data).decode('ascii')}}
    if type(value) is dict:
        if '$media' in value:
            raise ModelPreflightError('MODEL_WIRE_RESERVED_KEY')
        return {key: _encode(item) for key, item in value.items()}
    if type(value) is list:
        return [_encode(item) for item in value]
    return value


def _decode(value):
    if type(value) is dict:
        if '$media' in value:
            media = value['$media']
            if (set(value) != {'$media'} or type(media) is not dict
                    or set(media) != {'mime_type', 'base64'}):
                raise ModelPreflightError('INVALID_MODEL_MEDIA')
            try:
                return Media(base64.b64decode(media['base64'], validate=True), media['mime_type'])
            except (ValueError, TypeError):
                raise ModelPreflightError('INVALID_MODEL_MEDIA') from None
        return {key: _decode(item) for key, item in value.items()}
    if type(value) is list:
        return [_decode(item) for item in value]
    return value


def error_details(error, *, dispatched=None, result=_UNOBSERVED, trace=None):
    cause = getattr(error, '__cause__', None)
    details = {key: getattr(error, key, getattr(cause, key, None)) for key in ERROR_FIELDS}
    code = getattr(error, 'code', None)
    details['code'] = code if isinstance(code, str) else (
        str(error) if re.fullmatch('[A-Z][A-Z0-9_]{0,99}', str(error)) else 'MODEL_EXECUTION_FAILED')
    if isinstance(error, ModelPreflightError) and details['model_sent'] is not True:
        details.update(preflight_code=error.code, model_sent=False)
    if result is not _UNOBSERVED:
        details['model_sent'] = True
        observed = result if isinstance(result, dict) else {
            'usage': getattr(result, 'usage', None), 'request_id': getattr(result, 'request_id', None),
            'physical_request_id': getattr(result, 'physical_request_id', None)}
        if isinstance(observed.get('usage'), dict):
            details['observed_usage'] = observed['usage']
        if details['physical_request_id'] is None:
            details['physical_request_id'] = observed.get('physical_request_id')
        if details['request_id'] is None:
            details['request_id'] = observed.get('request_id')
    elif details['model_sent'] is None and dispatched is not None:
        details['model_sent'] = dispatched
    if details['model_sent'] is True:
        details['preflight_code'] = None
    if trace is not None:
        details['trace'] = trace
        if details['physical_request_id'] is None and isinstance(trace.get('last_physical_request_id'), str):
            details['physical_request_id'] = trace['last_physical_request_id']
    if isinstance(error, TerminalContractError):
        details['terminal'] = True
    return details


def model_error(details):
    if details.get('terminal'):
        error = TerminalContractError(details['code'])
    elif details.get('preflight_code') and details.get('model_sent') is not True:
        error = ModelPreflightError(details['preflight_code'])
    else:
        error = RemoteModelError(details['code'], details.get('status'),
                                 retry_after_seconds=details.get('retry_after_seconds'))
    for key in ERROR_FIELDS:
        if details.get(key) is not None:
            setattr(error, key, details[key])
    return error


class LlmEndpoint:
    def __init__(self, service, *, metadata=None, statistics=None, policy=None):
        self.service = service
        self._descriptors = {}
        catalog = service.catalog()
        for route in set(metadata or {}) | set(statistics or {}):
            bound = service.bind(route, sorted(catalog[route].capabilities)[0])
            self._descriptors[route] = (_registration(bound.registration_id),
                deepcopy((metadata or {}).get(route, {})), (statistics or {}).get(route))
        self._policy = policy
        self._wire = JsonEndpoint(self._dispatch, operations=OPERATIONS, result_error=self._result_error)

    def _result_error(self, request, response, error):
        executed = request['op'] in {'invoke', 'complete'}
        details = {'kind': 'model', **error_details(error, dispatched=executed,
            result=response['value'] if executed else _UNOBSERVED, trace=response.get('trace')),
            'code': 'MODEL_RESULT_UNAVAILABLE'}
        raise ContractError(details['code'], 'Model operation completed but its result could not be delivered',
                            status_code=502, details=details) from error

    def _descriptor(self, route, registration):
        configured = self._descriptors.get(route)
        if configured is not None and configured[0] == _registration(registration):
            return configured[1], configured[2]
        return {}, None

    def catalog(self, request):
        routes = {}
        for route, info in self.service.catalog().items():
            kind = sorted(info.capabilities)[0]
            bound = self.service.bind(route, kind)
            info = bound.info
            description = asdict(info)
            description.update(capabilities=sorted(kind.value for kind in info.capabilities),
                               input_modalities=sorted(info.input_modalities))
            with self.service.lease(route, kind, expected=bound.registration_id) as adapter:
                metadata, statistics = self._descriptor(route, bound.registration_id)
                metadata = {'manifest': getattr(adapter, 'manifest', None),
                    'embedding_manifest': getattr(adapter, 'embedding_manifest', None),
                    'dimension': getattr(adapter, 'dimension', None), **metadata}
                max_attempts = (self.service.completion_port(route, expected=bound.registration_id).max_attempts
                                if Capability.CHAT in info.capabilities else 1)
            routes[route] = {'info': description, 'registration': _registration(bound.registration_id),
                'metadata': metadata, 'max_attempts': max_attempts, 'statistics': statistics is not None}
        return {'protocol': PROTOCOL, 'routes': routes}

    def exchange(self, request):
        try:
            return self._wire.execute(request)
        except TransferError as error:
            raise ContractError(error.code, str(error), status_code=422) from error

    def _validate(self, contract, value):
        if self._policy is None:
            raise ModelPreflightError('MODEL_VALIDATION_UNAVAILABLE')
        try:
            self._policy.validate({'contract': contract, 'value': value})
        except ContractError as error:
            if error.details.get('terminal'):
                raise TerminalContractError(error.code) from error
            raise ValueError(error.code) from error

    def _dispatch(self, request):
        operation = request.get('op')
        validator = _VALIDATORS.get(operation)
        if validator is None or next(validator.iter_errors(request), None) is not None:
            raise ContractError('INVALID_LLM_OPERATION', 'Invalid model operation', status_code=422,
                                details={'kind': 'model', 'model_sent': False})
        kind = Capability(request['capability']) if operation == 'invoke' else (
            Capability.EMBEDDING if operation in {'recover', 'preflight'} else
            Capability.OCR if operation == 'statistics' else Capability.CHAT)
        trace = request.get('trace')
        started = False
        observed = _UNOBSERVED
        try:
            bound = self.service.bind(request['route'], kind)
            if _registration(bound.registration_id) != request['registration']:
                raise ModelPreflightError('MODEL_BINDING_CHANGED')
            with self.service.lease(request['route'], kind, expected=bound.registration_id) as adapter:
                if operation == 'invoke':
                    payload, context = _decode(request['payload']), dict(request['context'])
                    validation = request['validation']
                    if validation is not None:
                        if self._policy is None:
                            raise ModelPreflightError('MODEL_VALIDATION_UNAVAILABLE')
                        payload['validator'] = lambda value: self._validate(validation, value)
                    if self._policy is not None:
                        admission = {'provider': bound.info.provider, 'model': bound.info.model,
                                     'capability': kind.value, 'context': deepcopy(context)}

                        def authorize():
                            return self._policy.admit(admission)['allowed'] is True

                        if not authorize():
                            raise ModelPreflightError('MODEL_AUTHORIZATION_FAILED')
                        context['authorize'] = authorize
                    started = True
                    result = bound.invoke(ModelRequest(kind, payload, context))
                    observed = result
                    if self._policy is not None and not authorize():
                        error = RemoteModelError('MODEL_AUTHORIZATION_FAILED')
                        error.model_sent = True
                        raise error
                    value = {'provider': result.provider, 'model': result.model,
                        'capability': result.capability.value, 'data': _encode(result.data),
                        'usage': result.usage, 'request_id': result.request_id,
                        'physical_request_id': result.physical_request_id,
                        'normalized_usage': result.normalized_usage, 'normalized_finish': result.normalized_finish}
                elif operation == 'recover':
                    recover = getattr(adapter, 'recover_embedding', None)
                    value = recover(request['request_id']) if recover is not None else None
                elif operation == 'preflight':
                    preflight = getattr(adapter, 'embedding_preflight', None)
                    error = model_error(request['error'])
                    value = (preflight(request['request_id'], error) if preflight is not None
                             else getattr(error, 'preflight_code', None))
                elif operation == 'statistics':
                    _, statistics = self._descriptor(request['route'], bound.registration_id)
                    if statistics is None:
                        raise ModelPreflightError('MODEL_STATISTICS_UNAVAILABLE')
                    value = statistics()
                else:
                    port = self.service.completion_port(request['route'], expected=bound.registration_id)
                    if operation == 'identity':
                        value = port.identity(request['messages'], request['output_limit'])
                    elif operation == 'complete':
                        started = True
                        result = port.complete(request['messages'], request['output_limit'],
                            purpose=request['purpose'], key=request['key'], trace=trace)
                        observed = result
                        value = asdict(result)
                    elif operation == 'cache_get':
                        value = port.cache_get(request['key'])
                    elif operation == 'cache_put':
                        value = port.cache_put(request['key'], request['value'])
                    elif operation == 'record':
                        value = port.record(request['purpose'], request['key'], **request['metadata'])
                    else:
                        value = port.save_failed_response(ProviderCompletion(**request['result']), request['metadata'])
                return {'value': value, **({'trace': trace} if trace is not None else {})}
        except Exception as error:
            details = error_details(error, dispatched=started, result=observed, trace=trace)
            raise ContractError(details['code'], str(error), status_code=502,
                                details={'kind': 'model', **details}) from error

    def close(self):
        self._wire.close()


class LlmClient:
    def __init__(self, dependency):
        self._dependency = dependency
        self._wire = JsonClient(dependency.exchange, operations=OPERATIONS)

    def describe(self):
        result = self._dependency.catalog({})
        if result.get('protocol') != PROTOCOL:
            raise ModelPreflightError('MODEL_PROTOCOL_UNSUPPORTED')
        return result['routes']

    def catalog(self):
        return {name: ModelInfo(**row['info']) for name, row in self.describe().items()}

    def bind(self, route, capability):
        row = self.describe().get(route)
        if row is None:
            raise model_error({'code': 'MODEL_ROUTE_UNAVAILABLE', 'model_sent': False})
        return LlmBinding(self, route, Capability(capability), row)

    def invoke(self, route, request, *, validation=None):
        return self.bind(route, request.capability).invoke(request, validation=validation)

    def structured(self, route, *, policy=None, ledger=None):
        bound = self.bind(route, Capability.CHAT)
        return LlmStructuredModel(bound, policy=policy, ledger=ledger)

    def _call(self, request):
        try:
            return self._wire.call({'version': 1, **request})
        except ContractError as error:
            details = {'code': error.code, **error.details}
            if details.get('model_sent') is None and getattr(error, 'request_dispatched', None) is False:
                details.update(model_sent=False, preflight_code=details['code'])
            converted = model_error(details)
            if 'trace' in details:
                converted.trace = details['trace']
            raise converted from error
        except TransferError as error:
            converted = (RemoteModelError(error.code) if getattr(error, 'request_dispatched', True)
                         else ModelPreflightError(error.code))
            converted.model_sent = getattr(error, 'request_dispatched', None)
            raise converted from error


class LlmBinding:
    def __init__(self, client, route, capability, descriptor):
        self._client, self.route, self.capability = client, route, capability
        self.info = ModelInfo(**descriptor['info'])
        if capability not in self.info.capabilities:
            raise model_error({'code': 'MODEL_CAPABILITY_UNAVAILABLE', 'model_sent': False})
        self.registration_id = deepcopy(descriptor['registration'])
        self.metadata = deepcopy(descriptor['metadata'])
        self.max_attempts, self.statistics = descriptor['max_attempts'], descriptor['statistics']

    def call(self, operation, **options):
        return self._client._call({'op': operation, 'route': self.route,
                                  'registration': self.registration_id, **options})

    def invoke(self, request, *, validation=None):
        request = validate_request(request)
        if request.capability != self.capability:
            raise ModelPreflightError('MODEL_BINDING_CAPABILITY_MISMATCH')
        payload, context = dict(request.payload), dict(request.call_context)
        if payload.pop('validator', None) is not None or context.pop('authorize', None) is not None:
            raise ModelPreflightError('MODEL_CALLBACK_NOT_PORTABLE')
        if validation is not None and (not isinstance(validation, str) or not validation
                                      or self.capability != Capability.OCR):
            raise ModelPreflightError('MODEL_VALIDATION_CONTRACT_REQUIRED')
        response = self.call('invoke', capability=self.capability.value, payload=_encode(payload),
                             context=context, validation=validation)['value']
        try:
            result = ModelResult(**{**response, 'capability': Capability(response['capability']),
                                    'data': _decode(response['data'])})
            if (result.provider != self.info.provider or result.model != self.info.model
                    or result.capability != self.capability):
                raise RemoteModelError('INVALID_MODEL_RESULT_IDENTITY')
            validate_result(request, result)
        except Exception as error:
            raise model_error(error_details(error, result=response)) from error
        return result


class LlmCompletionPort:
    def __init__(self, binding):
        self._binding, self.max_attempts = binding, binding.max_attempts

    def identity(self, messages, output_limit):
        return self._binding.call('identity', messages=messages, output_limit=output_limit)['value']

    def complete(self, messages, output_limit, *, purpose, key, trace):
        try:
            response = self._binding.call('complete', messages=messages, output_limit=output_limit,
                                          purpose=purpose, key=key, trace=trace)
        except BaseException as error:
            if hasattr(error, 'trace'):
                trace.update(error.trace)
            elif getattr(error, 'model_sent', None) is not False:
                trace['attempt'] += 1
            raise
        try:
            trace.update(response['trace'])
            return ProviderCompletion(**response['value'])
        except Exception as error:
            if not trace.get('attempt'):
                trace['attempt'] = 1
            raise model_error(error_details(error, result=response.get('value'), trace=trace)) from error

    def cache_get(self, key):
        return self._binding.call('cache_get', key=key)['value']

    def cache_put(self, key, value):
        self._binding.call('cache_put', key=key, value=value)

    def record(self, purpose, key, **metadata):
        return self._binding.call('record', purpose=purpose, key=key, metadata=metadata)['value']

    def save_failed_response(self, result, metadata):
        return self._binding.call('diagnostic', result=asdict(result), metadata=metadata)['value']


class LlmStructuredModel:
    def __init__(self, binding, *, policy=None, ledger=None):
        self.provider = LlmCompletionPort(binding)
        self._model = AgentModel(self.provider, policy=policy, ledger=ledger)
        self.policy, self.ledger, self.capabilities = self._model.policy, self._model.ledger, self._model.capabilities
        self.provider_id, self.max_attempts = binding.info.provider, binding.max_attempts
        self.manifest = {'provider': binding.info.provider, 'model': binding.info.model,
                         'revision': binding.info.revision, 'protocol': PROTOCOL}

    def complete(self, messages, **options):
        return self._model.complete(messages, **options)

    def complete_json(self, messages, **options):
        return self.complete(messages, **options).to_dict()

    def accept_completion(self, envelope):
        self._model.accept_completion(envelope)

    def turn_budget_snapshot(self, session_id, turn_id):
        return self._model.turn_budget_snapshot(session_id, turn_id)
