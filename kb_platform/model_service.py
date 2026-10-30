from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
import math
from threading import RLock
from typing import Protocol

from .agent_engine import current_execution, execution_scope
from .agent_model import AgentModel
from .model_errors import RemoteModelError


class Capability(StrEnum):
    CHAT = 'chat'
    EMBEDDING = 'embedding'
    OCR = 'ocr'
    TRANSCRIPTION = 'transcription'
    SPEECH = 'speech'
    IMAGE = 'image'


@dataclass(frozen=True)
class Media:
    data: bytes
    mime_type: str

    def __post_init__(self):
        if (not isinstance(self.data, bytes) or not self.data or len(self.data) > 32 * 1024 * 1024
                or not isinstance(self.mime_type, str) or '/' not in self.mime_type):
            raise ValueError('INVALID_MODEL_MEDIA')


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    capabilities: frozenset[Capability]
    input_modalities: frozenset[str] = frozenset({'text'})
    streaming: bool = False
    revision: str = '1'
    preferred_batch_size: int = 1

    def __post_init__(self):
        if (not isinstance(self.provider, str) or not self.provider or not isinstance(self.model, str)
                or not self.model or not self.capabilities
                or type(self.preferred_batch_size) is not int or self.preferred_batch_size < 1):
            raise ValueError('INVALID_MODEL_INFO')
        object.__setattr__(self, 'capabilities', frozenset(Capability(x) for x in self.capabilities))
        object.__setattr__(self, 'input_modalities', frozenset(self.input_modalities))


@dataclass(frozen=True)
class ModelRequest:
    capability: Capability
    payload: dict
    call_context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResult:

    provider: str
    model: str
    capability: Capability
    data: object
    usage: dict | None = None
    request_id: str | None = None


class ModelAdapter(Protocol):
    info: ModelInfo
    def invoke(self, request: ModelRequest) -> ModelResult: ...


def validate_request(request):
    if not isinstance(request, ModelRequest) or not isinstance(request.payload, dict):
        raise ValueError('INVALID_MODEL_REQUEST')
    kind, body = Capability(request.capability), request.payload
    if not isinstance(request.call_context, dict):
        raise ValueError('INVALID_MODEL_CALL_CONTEXT')
    remaining = request.call_context.get('remaining_seconds')
    if remaining is not None and (type(remaining) not in (int, float) or not math.isfinite(remaining)
                                  or remaining <= 0):
        raise ValueError('INVALID_MODEL_DEADLINE')
    if kind == Capability.CHAT:
        if not isinstance(body.get('messages'), list) or not body['messages']:
            raise ValueError('INVALID_MODEL_MESSAGES')
        for message in body['messages']:
            if (not isinstance(message, dict) or message.get('role') not in {'system', 'user', 'assistant'}
                    or not isinstance(message.get('content'), str)):
                raise ValueError('INVALID_MODEL_MESSAGES')
    elif kind == Capability.EMBEDDING:
        if (not isinstance(body.get('texts'), list) or not body['texts']
                or any(not isinstance(x, str) or not x for x in body['texts'])
                or body.get('task', 'document') not in {'document', 'query'}):
            raise ValueError('INVALID_EMBEDDING_INPUT')
    elif kind in {Capability.OCR, Capability.TRANSCRIPTION}:
        media = body.get('media')
        prefix = 'image/' if kind == Capability.OCR else 'audio/'
        if not isinstance(media, Media) or not media.mime_type.startswith(prefix):
            raise ValueError('INVALID_MODEL_MEDIA_TYPE')
    elif kind in {Capability.SPEECH, Capability.IMAGE}:
        key = 'text' if kind == Capability.SPEECH else 'prompt'
        if not isinstance(body.get(key), str) or not body[key].strip():
            raise ValueError('INVALID_MODEL_TEXT')
    allowed = {
        Capability.CHAT: {'messages', 'max_tokens'},
        Capability.EMBEDDING: {'texts', 'task', 'dimensions'},
        Capability.OCR: {'media', 'system', 'instruction', 'validator', 'contract_version', 'max_tokens'},
        Capability.TRANSCRIPTION: {'media', 'language'},
        Capability.SPEECH: {'text', 'voice'},
        Capability.IMAGE: {'prompt', 'count', 'size'},
    }
    if set(body) - allowed[kind]:
        raise ValueError('UNSUPPORTED_MODEL_OPTION')
    for key in ('max_tokens', 'dimensions', 'count'):
        if key in body and (type(body[key]) is not int or body[key] < 1):
            raise ValueError('INVALID_MODEL_LIMIT')
    if 'validator' in body and body['validator'] is not None and not callable(body['validator']):
        raise ValueError('INVALID_MODEL_VALIDATOR')
    return ModelRequest(kind, deepcopy(body), deepcopy(request.call_context))


def validate_result(request, result):
    data, kind = result.data, request.capability
    if not isinstance(data, dict) or result.usage is not None and not isinstance(result.usage, dict):
        raise RemoteModelError('INVALID_MODEL_RESULT')
    valid = True
    if kind in {Capability.CHAT, Capability.TRANSCRIPTION}:
        valid = isinstance(data.get('text'), str)
    elif kind == Capability.EMBEDDING:
        vectors = data.get('vectors')
        valid = (isinstance(vectors, list) and len(vectors) == len(request.payload['texts']) and
                 all(isinstance(vector, list) and vector and
                     all(type(x) in (int, float) and math.isfinite(x) for x in vector) for vector in vectors))
        if valid:
            dimension = request.payload.get('dimensions', len(vectors[0]))
            valid = all(len(v) == dimension for v in vectors)
    elif kind == Capability.SPEECH:
        valid = isinstance(data.get('media'), Media) and data['media'].mime_type.startswith('audio/')
    elif kind == Capability.IMAGE:
        images = data.get('images')
        valid = isinstance(images, list) and len(images) == request.payload.get('count', 1)
        if valid:
            valid = all(isinstance(item, dict) and (
                isinstance(item.get('media'), Media) and item['media'].mime_type.startswith('image/') or
                isinstance(item.get('url'), str) and item['url'].startswith('https://')) for item in images)
    if not valid:
        raise RemoteModelError('INVALID_MODEL_RESULT')
    if kind == Capability.OCR and request.payload.get('validator') is not None:
        request.payload['validator'](data)


class ModelService:
    def __init__(self):
        self._entries, self._active = {}, {}
        self._lock, self._closed = RLock(), False

    def register(self, name, adapter, *, owned=False):
        if not isinstance(name, str) or not name or not isinstance(adapter.info, ModelInfo):
            raise ValueError('INVALID_MODEL_REGISTRATION')
        with self._lock:
            if self._closed or name in self._entries:
                raise ValueError('MODEL_REGISTRATION_UNAVAILABLE')
            entry = (adapter, owned)
            self._entries[name] = entry
            self._active[name] = 0

        def dispose():
            with self._lock:
                if self._entries.get(name) is not entry:
                    return
                if self._active[name]:
                    raise ValueError('MODEL_BUSY')
                if owned:
                    adapter.close()
                del self._entries[name]
                del self._active[name]
        return dispose

    def catalog(self):
        with self._lock:
            return {key: adapter.info for key, (adapter, _) in self._entries.items()}

    @contextmanager
    def lease(self, name, capability, *, expected=None):
        with self._lock:
            if self._closed or name not in self._entries:
                raise RemoteModelError('MODEL_ROUTE_UNAVAILABLE')
            if expected is not None and self._entries[name] is not expected:
                raise RemoteModelError('MODEL_BINDING_CHANGED')
            adapter = self._entries[name][0]
            if capability not in adapter.info.capabilities:
                raise RemoteModelError('MODEL_CAPABILITY_UNAVAILABLE')
            self._active[name] += 1
        try:
            execution = current_execution()
            if execution is not None:
                execution.check()
            yield adapter
            if execution is not None:
                execution.check()
        finally:
            with self._lock:
                self._active[name] -= 1

    def invoke(self, name, request):
        return self._invoke(name, request)

    def bind(self, name, capability):

        capability = Capability(capability)
        with self._lock, self.lease(name, capability) as adapter:
            return BoundModel(self, name, capability, self._entries[name], adapter.info)

    def _invoke(self, name, request, *, expected=None):
        request = validate_request(request)
        remaining = request.call_context.get('remaining_seconds')
        scope = execution_scope(deadline_seconds=remaining) if remaining is not None else nullcontext()
        with scope, self.lease(name, request.capability, expected=expected) as adapter:
            result = adapter.invoke(request)
            if (not isinstance(result, ModelResult) or result.provider != adapter.info.provider
                    or result.model != adapter.info.model or result.capability != request.capability):
                raise RemoteModelError('INVALID_MODEL_RESULT_IDENTITY')
            validate_result(request, result)
            return result

    def structured(self, name, *, policy=None, ledger=None):

        with self.lease(name, Capability.CHAT) as adapter:
            if not callable(getattr(adapter, 'completion_port', None)):
                raise RemoteModelError('STRUCTURED_MODEL_UNAVAILABLE')
            model = AgentModel(adapter.completion_port(), policy=policy, ledger=ledger)
        return StructuredModel(self, name, adapter, model)

    def close(self):
        with self._lock:
            if any(self._active.values()):
                raise ValueError('MODEL_BUSY')
            for name in reversed(list(self._entries)):
                adapter, owned = self._entries[name]
                if owned:
                    adapter.close()
                del self._entries[name]
                del self._active[name]
            self._closed = True


class BoundModel:

    def __init__(self, service, name, capability, entry, info):
        self._service, self._name, self._capability, self._entry = service, name, capability, entry
        self.info = info

    def invoke(self, request):
        if not isinstance(request, ModelRequest) or request.capability != self._capability:
            raise ValueError('MODEL_BINDING_CAPABILITY_MISMATCH')
        return self._service._invoke(self._name, request, expected=self._entry)


class StructuredModel:
    def __init__(self, service, name, adapter, model):
        self._service, self._name, self._adapter, self._model = service, name, adapter, model
        self.policy, self.ledger, self.capabilities = model.policy, model.ledger, model.capabilities
        self.provider_id = adapter.info.provider
        self.manifest = {'provider': adapter.info.provider, 'model': adapter.info.model,
                         'revision': adapter.info.revision, 'protocol': 'model-service/1'}

    @property
    def max_attempts(self):
        return getattr(self._model.provider, 'max_attempts', 1)

    def complete(self, messages, **options):
        with self._service.lease(self._name, Capability.CHAT) as adapter:
            if adapter is not self._adapter:
                raise RemoteModelError('MODEL_BINDING_CHANGED')
            return self._model.complete(messages, **options)
