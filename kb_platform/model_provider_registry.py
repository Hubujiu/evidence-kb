from copy import deepcopy
from dataclasses import dataclass
import re
from threading import RLock

from jsonschema import Draft202012Validator

from .model_sdk import Capability, validate_adapter
from .plugin_configuration import config_schema, TEXT


@dataclass(frozen=True)
class ProviderAdapterDefinition:
    key: str
    capabilities: frozenset[Capability]
    options_schema: dict
    factory: object


class ProviderAdapterRegistry:
    def __init__(self):
        self._definitions, self._lock = {}, RLock()

    def register(self, key, *, capabilities, options_schema, factory):
        if not isinstance(key, str) or re.fullmatch('[a-z][a-z0-9-]{0,79}', key) is None or not callable(factory):
            raise ValueError('INVALID_PROVIDER_ADAPTER_REGISTRATION')
        supported = frozenset(Capability(value) for value in capabilities)
        schema = deepcopy(options_schema)
        Draft202012Validator.check_schema(schema)
        if not supported or schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
            raise ValueError('INVALID_PROVIDER_ADAPTER_REGISTRATION')
        definition = ProviderAdapterDefinition(key, supported, schema, factory)
        with self._lock:
            if key in self._definitions:
                raise ValueError('PROVIDER_ADAPTER_ALREADY_REGISTERED')
            self._definitions[key] = definition

    def definition(self, key):
        with self._lock:
            try:
                definition = self._definitions[key]
            except KeyError:
                raise ValueError('MODEL_ADAPTER_UNAVAILABLE') from None
            return ProviderAdapterDefinition(definition.key, definition.capabilities,
                                             deepcopy(definition.options_schema), definition.factory)

    def provider_schema(self):
        with self._lock:
            definitions = tuple(self._definitions.values())
        return {'oneOf': [config_schema({
            'adapter': {'const': definition.key}, 'base_url': TEXT,
            'api_key_env': {'type': 'string', 'pattern': '^[A-Za-z_][A-Za-z0-9_]*$'},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0},
            'options': deepcopy(definition.options_schema)},
            required=('adapter', 'base_url', 'api_key_env')) for definition in definitions]}

    def create(self, key, *, provider_options, **options):
        definition = self.definition(key)
        adapter = definition.factory(provider_options=deepcopy(provider_options), **options)
        try:
            info = validate_adapter(adapter, owned=True)
            if (info.provider != options['provider'] or info.model != options['model']
                    or info.capabilities != frozenset(Capability(value) for value in options['capabilities'])
                    or not info.capabilities <= definition.capabilities):
                raise ValueError('MODEL_ADAPTER_IDENTITY_MISMATCH')
        except BaseException:
            adapter.close()
            raise
        return adapter


def _openai_factory(*, provider_options, **options):
    from .model_http import OpenAICompatibleAdapter

    return OpenAICompatibleAdapter(**options)


def _anthropic_factory(*, provider_options, **options):
    from .model_http import AnthropicAdapter

    return AnthropicAdapter(**options)


PROVIDER_ADAPTERS = ProviderAdapterRegistry()
PROVIDER_ADAPTERS.register('openai-compatible', capabilities=Capability,
                         options_schema=config_schema({}), factory=_openai_factory)
PROVIDER_ADAPTERS.register('anthropic', capabilities=[Capability.CHAT],
                         options_schema=config_schema({}), factory=_anthropic_factory)


def register_provider_adapter(key, *, capabilities, options_schema, factory):
    PROVIDER_ADAPTERS.register(key, capabilities=capabilities, options_schema=options_schema, factory=factory)
