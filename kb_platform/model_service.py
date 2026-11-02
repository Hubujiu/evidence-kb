from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from inspect import iscoroutine
from threading import RLock
from uuid import uuid4

from .agent_engine import current_execution, execution_scope
from .agent_model import AgentModel
from .model_errors import RemoteModelError
from .model_sdk import (
    MODEL_PROTOCOL, Capability as Capability, Media as Media, ModelInfo as ModelInfo,
    ModelRequest as ModelRequest, ModelResult as ModelResult, ModelAdapter as ModelAdapter,
    validate_adapter, validate_request as validate_request, validate_result as validate_result,
)


class RoutePhase(StrEnum):
    ACTIVE = 'active'
    DRAINING = 'draining'
    CLEANING = 'cleaning'
    CLEANUP_FAILED = 'cleanup_failed'


@dataclass(eq=False)
class _Route:
    registration_id: object
    owner_identity: object
    adapter: ModelAdapter
    info: ModelInfo
    owned: bool
    owner_lease: object
    phase: RoutePhase = RoutePhase.ACTIVE
    active: int = 0
    cleanup_error: str | None = None


class ModelService:
    def __init__(self, *, owner_identity=None):
        self.owner_identity = self._freeze_owner(owner_identity)
        self._entries = {}
        self._issued_ids = set()
        self._lock, self._state = RLock(), 'active'

    @staticmethod
    def _freeze_owner(identity):
        return tuple(sorted(dict(identity).items())) if identity is not None else None

    def register(self, name, adapter, *, owned=False, registration_id=None,
                 owner_identity=None, owner_lease=None):
        info = validate_adapter(adapter, owned=owned)
        if not isinstance(name, str) or not name:
            raise ValueError('INVALID_MODEL_REGISTRATION')
        raw_identity = registration_id if registration_id is not None else uuid4().hex
        if not (isinstance(raw_identity, str) and raw_identity or
                type(raw_identity) is int and raw_identity > 0):
            raise ValueError('INVALID_MODEL_REGISTRATION_ID')
        owner = self._freeze_owner(owner_identity)
        identity = (owner, raw_identity) if owner is not None else raw_identity
        entry = _Route(identity, owner, adapter, info, owned, owner_lease)
        with self._lock:
            if self._state != 'active' or name in self._entries:
                raise ValueError('MODEL_REGISTRATION_UNAVAILABLE')
            if identity in self._issued_ids:
                raise ValueError('MODEL_REGISTRATION_ID_REUSED')
            self._entries[name] = entry
            self._issued_ids.add(identity)
        return lambda: self._dispose(name, entry)

    def adopt(self, owner):
        with self._lock:
            if self._state != 'active' or any(row.active for row in self._entries.values()):
                raise ValueError('MODEL_BUSY')
            rows = list(self._entries.values())
        identity = self._freeze_owner(owner.identity)
        identities = [(identity, owner.registration_id(kind='model_route')) for _ in rows]
        with self._lock:
            if list(self._entries.values()) != rows or any(row.active for row in rows):
                raise ValueError('MODEL_REGISTRATION_CHANGED')
            if len(set(identities)) != len(identities) or self._issued_ids.intersection(identities):
                raise ValueError('MODEL_REGISTRATION_ID_REUSED')
            self.owner_identity = identity
            for row, registration in zip(rows, identities):
                row.registration_id, row.owner_identity, row.owner_lease = registration, identity, owner.lease
            self._issued_ids.update(identities)

    def _dispose(self, name, entry):
        with self._lock:
            if self._entries.get(name) is not entry:
                return
            if entry.phase == RoutePhase.CLEANING:
                raise ValueError('MODEL_CLEANUP_BUSY')
            entry.phase = RoutePhase.DRAINING
            if entry.active:
                raise ValueError('MODEL_BUSY')
            entry.phase = RoutePhase.CLEANING
        try:
            if entry.owned:
                result = entry.adapter.close()
                if result is not None:
                    if iscoroutine(result):
                        result.close()
                    raise ValueError('INVALID_MODEL_CLEANUP_RESULT')
        except BaseException as error:
            with self._lock:
                entry.phase, entry.cleanup_error = RoutePhase.CLEANUP_FAILED, type(error).__name__
            raise
        with self._lock:
            del self._entries[name]

    def catalog(self):
        with self._lock:
            return {key: entry.info for key, entry in self._entries.items()
                    if entry.phase == RoutePhase.ACTIVE}

    def inspect(self):
        with self._lock:
            return {name: {'registration_id': row.registration_id,
                           'owner_identity': deepcopy(row.owner_identity), 'phase': row.phase,
                           'active_calls': row.active, 'cleanup_error': row.cleanup_error}
                    for name, row in self._entries.items()}

    def _entry(self, name, capability, expected=None):
        entry = self._entries.get(name)
        if self._state != 'active' or entry is None or entry.phase != RoutePhase.ACTIVE:
            raise RemoteModelError('MODEL_ROUTE_UNAVAILABLE')
        if expected is not None and entry.registration_id != expected:
            raise RemoteModelError('MODEL_BINDING_CHANGED')
        if capability not in entry.info.capabilities:
            raise RemoteModelError('MODEL_CAPABILITY_UNAVAILABLE')
        return entry

    @contextmanager
    def lease(self, name, capability, *, expected=None):
        with self._lock:
            entry = self._entry(name, capability, expected)
            identity, owner_lease = entry.registration_id, entry.owner_lease
        ownership = owner_lease() if owner_lease is not None else nullcontext()
        with ownership:
            with self._lock:
                entry = self._entry(name, capability, expected=identity)
                entry.active += 1
            try:
                execution = current_execution()
                if execution is not None:
                    execution.check()
                yield entry.adapter
                if execution is not None:
                    execution.check()
            finally:
                with self._lock:
                    entry.active -= 1

    def invoke(self, name, request):
        return self._invoke(name, request)

    def bind(self, name, capability):
        capability = Capability(capability)
        with self._lock:
            entry = self._entry(name, capability)
            return BoundModel(self, name, capability, entry.registration_id, entry.info)

    def _invoke(self, name, request, *, expected=None):
        request = validate_request(request)
        with self._lock:
            entry = self._entry(name, request.capability, expected)
            identity, info = entry.registration_id, entry.info
        remaining = request.call_context.get('remaining_seconds')
        scope = execution_scope(deadline_seconds=remaining) if remaining is not None else nullcontext()
        with scope, self.lease(name, request.capability, expected=identity) as adapter:
            result = adapter.invoke(request)
            try:
                if (not isinstance(result, ModelResult) or result.provider != info.provider
                        or result.model != info.model or result.capability != request.capability):
                    raise RemoteModelError('INVALID_MODEL_RESULT_IDENTITY')
                validate_result(request, result)
            except Exception as error:
                error.model_sent = True
                raise
            return result

    def structured(self, name, *, policy=None, ledger=None):
        with self._lock:
            entry = self._entry(name, Capability.CHAT)
        with self.lease(name, Capability.CHAT, expected=entry.registration_id) as adapter:
            from .model_completion import CompletionPort, validate_completion_port

            factory = getattr(adapter, 'completion_port', None)
            port = (factory() if factory is not None else
                    CompletionPort(adapter, invoke=self.bind(name, Capability.CHAT).invoke))
            model = AgentModel(validate_completion_port(port), policy=policy, ledger=ledger)
            return StructuredModel(self, name, entry, model)

    def close(self):
        with self._lock:
            if any(entry.active for entry in self._entries.values()):
                raise ValueError('MODEL_BUSY')
            self._state = 'draining'
            pending = list(reversed(list(self._entries.items())))
        for name, entry in pending:
            self._dispose(name, entry)
        with self._lock:
            self._state = 'closed'


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
        return self._model.provider

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
