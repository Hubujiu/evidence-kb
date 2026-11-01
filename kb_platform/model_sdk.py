from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
import math
from typing import Protocol

from .model_errors import RemoteModelError
from .protocol_validation import callable_accepts


MODEL_PROTOCOL = 'model-service/1'


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
    protocol: str = MODEL_PROTOCOL

    def __post_init__(self):
        if (not isinstance(self.provider, str) or not self.provider or not isinstance(self.model, str)
                or not self.model or not self.capabilities
                or not isinstance(self.revision, str) or not self.revision
                or not isinstance(self.protocol, str) or not self.protocol
                or type(self.streaming) is not bool
                or type(self.preferred_batch_size) is not int or self.preferred_batch_size < 1):
            raise ValueError('INVALID_MODEL_INFO')
        object.__setattr__(self, 'capabilities', frozenset(Capability(x) for x in self.capabilities))
        modalities = frozenset(self.input_modalities)
        if not modalities or not modalities <= {'text', 'image', 'audio'}:
            raise ValueError('INVALID_MODEL_MODALITIES')
        object.__setattr__(self, 'input_modalities', modalities)


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


class OwnedModelAdapter(ModelAdapter, Protocol):
    def close(self) -> None: ...


class StructuredModelAdapter(ModelAdapter, Protocol):
    def completion_port(self): ...


def validate_adapter(adapter, *, owned=False):
    info = getattr(adapter, 'info', None)
    if not isinstance(info, ModelInfo):
        raise ValueError('INVALID_MODEL_REGISTRATION')
    if info.protocol != MODEL_PROTOCOL:
        raise ValueError('MODEL_PROTOCOL_UNSUPPORTED')
    if not callable_accepts(getattr(adapter, 'invoke', None), ModelAdapter.invoke):
        raise ValueError('INVALID_MODEL_ADAPTER_INVOKE')
    if owned and not callable_accepts(getattr(adapter, 'close', None), OwnedModelAdapter.close):
        raise ValueError('INVALID_MODEL_ADAPTER_CLOSE')
    factory = getattr(adapter, 'completion_port', None)
    if factory is not None and not callable_accepts(factory, StructuredModelAdapter.completion_port):
        raise ValueError('INVALID_MODEL_COMPLETION_PORT')
    return info


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
    authorize = request.call_context.get('authorize')
    if authorize is not None and not callable(authorize):
        raise ValueError('INVALID_MODEL_AUTHORIZER')
    references = {id(value): value for value in (body.get('validator'), authorize) if callable(value)}
    return ModelRequest(kind, deepcopy(body, references), deepcopy(request.call_context, references))


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


