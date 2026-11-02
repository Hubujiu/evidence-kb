from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from functools import partial
from inspect import iscoroutine
from threading import RLock

from .agent_engine import current_execution, execution_scope
from .agent_model import AgentModel
from .llm_protocol import CompletionBridge, NativeModelRegistry, chat_metadata
from .model_sdk import (
    MODEL_PROTOCOL, Capability as Capability, Media as Media, ModelInfo as ModelInfo,
    ModelRequest as ModelRequest, ModelResult as ModelResult, ModelAdapter as ModelAdapter,
    preserve_result_failure, validate_adapter, validate_request as validate_request, validate_result as validate_result,
)


@dataclass
class _AdapterHandle:
    adapter: ModelAdapter
    owned: bool
    owner_lease: object


class ModelService:
    def __init__(self, *, owner_identity=None):
        self.owner_identity = self._freeze_owner(owner_identity)
        self._registry = NativeModelRegistry()
        self._adapters = {}
        self._lock = RLock()

    @staticmethod
    def _freeze_owner(identity):
        return tuple(sorted(dict(identity).items())) if identity is not None else None

    @classmethod
    def _identity(cls, route):
        identity = route['identity']
        owner = cls._freeze_owner(identity['owner'])
        return (owner, identity['registrationId']) if owner is not None else identity['registrationId']

    @staticmethod
    def _expected(identity):
        if identity is None:
            return None
        if isinstance(identity, tuple) and len(identity) == 2:
            owner, registration = identity
            return {'owner': dict(owner), 'registrationId': registration}
        return {'owner': None, 'registrationId': identity}

    @staticmethod
    def _info(route):
        return ModelInfo(**route['info'])

    def register(self, name, adapter, *, owned=False, registration_id=None,
                 owner_identity=None, owner_lease=None):
        info = validate_adapter(adapter, owned=owned)
        if not isinstance(name, str) or not name:
            raise ValueError('INVALID_MODEL_REGISTRATION')
        descriptor = asdict(info)
        descriptor['capabilities'] = sorted(info.capabilities)
        descriptor['input_modalities'] = sorted(info.input_modalities)
        identity = (None if registration_id is None and owner_identity is None else
                    {'registrationId': registration_id, 'owner': dict(owner_identity) if owner_identity is not None else None})
        handle = _AdapterHandle(adapter, owned, owner_lease)
        with self._lock:
            result = self._registry.call('route_register', name=name, info=descriptor, identity=identity)
            binding = result['binding']
            self._adapters[binding['registration']] = handle
        return lambda: self._dispose(name, binding)

    def adopt(self, owner):
        with self._lock:
            routes = self._registry.call('service_inspect')['routes']
        identity = dict(owner.identity)
        identities = [{'registration': row['binding']['registration'],
                       'identity': {'owner': identity, 'registrationId': owner.registration_id(kind='model_route')}}
                      for row in routes]
        with self._lock:
            adopted = self._registry.call('route_adopt', identities=identities)['routes']
            for row in adopted:
                self._adapters[row['binding']['registration']].owner_lease = owner.lease
            self.owner_identity = self._freeze_owner(identity)

    def _dispose(self, name, binding):
        with self._lock:
            if self._registry.handle is None:
                return
            route = self._registry.call('route_begin_cleanup', name=name, binding=binding)['route']
            if route is None:
                return
            handle = self._adapters[binding['registration']]
        try:
            if handle.owned:
                result = handle.adapter.close()
                if result is not None:
                    if iscoroutine(result):
                        result.close()
                    raise ValueError('INVALID_MODEL_CLEANUP_RESULT')
        except BaseException as error:
            self._registry.call('route_finish_cleanup', binding=binding, error=type(error).__name__)
            raise
        with self._lock:
            self._registry.call('route_finish_cleanup', binding=binding, error=None)
            del self._adapters[binding['registration']]

    def catalog(self):
        return {row['name']: self._info(row) for row in self._registry.call('service_inspect')['routes']
                if row['phase'] == 'active'}

    def inspect(self):
        return {row['name']: {'registration_id': self._identity(row),
                'owner_identity': self._freeze_owner(row['identity']['owner']), 'phase': row['phase'],
                'active_calls': row['activeCalls'], 'cleanup_error': row['cleanupError']}
                for row in self._registry.call('service_inspect')['routes']}

    def _bound(self, name, capability, expected=None):
        return self._registry.call('route_bind', lookup=True, name=name, capability=str(capability),
                                   expected=self._expected(expected))

    @contextmanager
    def _admit(self, name, capability, *, expected=None, request=None):
        with self._lock:
            route = self._bound(name, capability, expected)
            handle = self._adapters[route['binding']['registration']]
        ownership = handle.owner_lease() if handle.owner_lease is not None else nullcontext()
        with ownership:
            info = self._info(route)
            metadata = (chat_metadata(info.provider, info.model, request.payload['messages'],
                        max_tokens=request.payload.get('max_tokens'), options=request.payload)
                        if request is not None and capability == Capability.CHAT else None)
            prepared = self._registry.call('call_prepare', lookup=True, name=name, capability=str(capability),
                expected=route['identity'], kind='invoke' if request is not None else 'ownership', request=metadata)
            call = self._registry.call('call_begin', lookup=True, prepared=prepared['prepared'])['call']
            try:
                execution = current_execution()
                if execution is not None:
                    execution.check()
                yield handle.adapter, info, call
                if execution is not None:
                    execution.check()
            finally:
                self._registry.call('call_release', call=call)

    @contextmanager
    def lease(self, name, capability, *, expected=None):
        with self._admit(name, Capability(capability), expected=expected) as (adapter, _, _):
            yield adapter

    def invoke(self, name, request):
        return self._invoke(name, request)

    def bind(self, name, capability):
        capability = Capability(capability)
        route = self._bound(name, capability)
        return BoundModel(self, name, capability, self._identity(route), self._info(route))

    def _invoke(self, name, request, *, expected=None):
        request = validate_request(request)
        remaining = request.call_context.get('remaining_seconds')
        scope = execution_scope(deadline_seconds=remaining) if remaining is not None else nullcontext()
        started, received, result = False, False, None
        try:
            with scope, self._admit(name, request.capability, expected=expected, request=request) as (adapter, info, call):
                started = True
                result = adapter.invoke(request)
                received = True
                validate_result(request, result, info=info)
                chat = request.capability == Capability.CHAT
                normalized = self._registry.call('call_complete', sent=True, call=call, result={
                    'provider': result.provider, 'model': result.model, 'capability': str(result.capability),
                    'textIsText': isinstance(result.data['text'], str) if chat else None,
                    'finishReason': result.data.get('finish_reason') if chat else None, 'usage': result.usage})
                if chat:
                    result = replace(result, normalized_usage=normalized['usage'],
                                     normalized_finish=normalized['finish'])
                return result
        except Exception as error:
            if received:
                preserve_result_failure(error, getattr(result, 'usage', None), result=result)
            elif not started:
                error.model_sent = False
            raise

    def completion_port(self, name, *, expected=None):
        from .model_completion import CompletionPort, validate_completion_port

        bound = self._bound(name, Capability.CHAT, expected)
        identity = self._identity(bound)
        with self.lease(name, Capability.CHAT, expected=identity) as adapter:
            factory = getattr(adapter, 'completion_port', None)
            port = validate_completion_port(factory() if factory is not None else
                                            CompletionPort(adapter, info=self._info(bound)))
        return CompletionBridge(port, partial(self._complete_port, name, identity),
                                partial(self.lease, name, Capability.CHAT, expected=identity))

    def _complete_port(self, name, expected, port, messages, output_limit, *, purpose, key, trace):
        request = validate_request(ModelRequest(Capability.CHAT,
                                   {'messages': messages, 'max_tokens': output_limit}, trace))
        started, received, result = False, False, None
        try:
            with self._admit(name, Capability.CHAT, expected=expected, request=request) as (_, info, call):
                started = True
                result = port.complete(request.payload['messages'], output_limit, purpose=purpose, key=key, trace=trace)
                received = True
                if isinstance(result.content, str):
                    self._registry.call('call_complete', sent=True, call=call, result={
                        'provider': info.provider, 'model': info.model, 'capability': 'chat',
                        'textIsText': True, 'finishReason': result.finish_reason, 'usage': result.usage})
                return result
        except Exception as error:
            if received:
                preserve_result_failure(error, getattr(result, 'usage', None), result=result,
                                        physical_request_id=trace.get('last_physical_request_id'))
            elif not started:
                error.model_sent = False
            raise

    def structured(self, name, *, policy=None, ledger=None):
        bound = self.bind(name, Capability.CHAT)
        port = self.completion_port(name, expected=bound.registration_id)
        return StructuredModel(self, name, bound, AgentModel(port, policy=policy, ledger=ledger))

    def close(self):
        with self._lock:
            if self._registry.handle is None:
                return
            pending = self._registry.call('service_begin_close')['routes']
        for route in pending:
            self._dispose(route['name'], route['binding'])
        with self._lock:
            self._registry.call('service_finish_close')


class BoundModel:

    def __init__(self, service, name, capability, entry, info):
        self._service, self._name, self._capability, self._entry = service, name, capability, entry
        self.info = info

    @property
    def registration_id(self):
        return self._entry

    def invoke(self, request):
        if not isinstance(request, ModelRequest) or request.capability != self._capability:
            raise ValueError('MODEL_BINDING_CAPABILITY_MISMATCH')
        return self._service._invoke(self._name, request, expected=self._entry)


class StructuredModel:
    def __init__(self, service, name, entry, model):
        self._service, self._name, self._entry, self._model = service, name, entry.registration_id, model
        self.policy, self.ledger, self.capabilities = model.policy, model.ledger, model.capabilities
        self.provider_id = entry.info.provider
        self.manifest = {'provider': entry.info.provider, 'model': entry.info.model,
                         'revision': entry.info.revision, 'protocol': MODEL_PROTOCOL}

    @property
    def provider(self):
        return self._model.provider.unwrap()

    @property
    def registration_id(self):
        return self._entry

    @property
    def max_attempts(self):
        return getattr(self._model.provider, 'max_attempts', 1)

    def complete(self, messages, **options):
        with self._service.lease(self._name, Capability.CHAT, expected=self._entry):
            return self._model.complete(messages, **options)

    def complete_json(self, messages, **options):
        return self.complete(messages, **options).to_dict()

    def turn_budget_snapshot(self, session_id, turn_id):
        with self._service.lease(self._name, Capability.CHAT, expected=self._entry):
            return self._model.turn_budget_snapshot(session_id, turn_id)

    def accept_completion(self, envelope):
        with self._service.lease(self._name, Capability.CHAT, expected=self._entry):
            self._model.accept_completion(envelope)
