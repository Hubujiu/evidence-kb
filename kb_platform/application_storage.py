from dataclasses import asdict
from contextlib import contextmanager
from threading import RLock

from .database_contracts import CommitUnknown, DatabaseError
from .plugin_contracts import ContractError, Method
from .storage_process import decode_value, encode_value
from .storage_sdk import (DOCUMENT_V1, EphemeralCacheSpace, ExistingDataset,
                          LEXICAL_BM25_V1, OBJECT_V1, SpaceKey, StorageError, VECTOR_V1)
from .storage_sdk.relational import GUARDED_RELATIONAL_V1


REQUIREMENTS = {
    'repository': {'primary': GUARDED_RELATIONAL_V1},
    'store': {'primary': GUARDED_RELATIONAL_V1},
    'provider': {'primary': GUARDED_RELATIONAL_V1},
    'search': {'bm25': LEXICAL_BM25_V1, 'dense': VECTOR_V1, 'source': DOCUMENT_V1},
    'originals': {'objects': OBJECT_V1},
}


def storage_methods():
    key = {'type': 'string', 'minLength': 1}
    def request(properties):
        return {'type': 'object', 'properties': properties,
                'required': list(properties), 'additionalProperties': False}
    return {
        'describe': Method(request({'binding': key}), {'type': 'object'}),
        'invoke': Method(request({'binding': key, 'method': key,
            'args': {'type': 'array'}, 'kwargs': {'type': 'object'}}), {'type': 'object'}),
        'relational': Method(request({'binding': key, 'request': {'type': 'object'}}), {'type': 'object'}),
        'objects': Method(request({'binding': key, 'request': {'type': 'object'}}), {'type': 'object'}),
    }


class ApplicationStorage:
    def __init__(self, configuration, *, provider_catalog=None):
        from .plugin_host import PluginSpec
        from .storage_catalog import default_storage_providers
        from .storage_plugins import compile_storage_plugins
        from .storage_sdk import StorageSlot
        from .storage_service import StorageService

        providers = default_storage_providers() if provider_catalog is None else provider_catalog
        consumers = configuration.get('bindings', {})
        if set(consumers) - set(REQUIREMENTS):
            raise ValueError('UNKNOWN_APPLICATION_STORAGE_CONSUMER')
        specs = tuple(PluginSpec(name, (), tuple(StorageSlot(slot, contract)
            for slot, contract in REQUIREMENTS[name].items()), lambda ctx, config: None)
            for name in consumers)
        compile_storage_plugins({'version': 1, 'plugins': [{'id': name, 'use': name} for name in consumers]},
            specs, configuration, provider_catalog=providers)
        self._registry, self._instances, self._bound, self._structured = StorageService(), [], {}, {}
        self._objects, self._lock, self._active, self._closing = {}, RLock(), 0, False
        try:
            exports = {}
            for name, definition in configuration['instances'].items():
                provider = providers[definition['provider']]
                instance = provider.factory(definition['config'])
                self._instances.append(instance)
                actual = {item.name: item for item in instance.exports()}
                if set(actual) != set(provider.exports) or any(
                        actual[key].contracts != tuple(value) for key, value in provider.exports.items()):
                    raise StorageError('STORAGE_PROVIDER_EXPORT_MISMATCH')
                exports[name] = actual
            for name, definition in configuration['services'].items():
                self._registry.register(name, exports[definition['instance']][definition['export']])
            for consumer, bindings in consumers.items():
                for alias, binding in bindings.items():
                    space = SpaceKey(binding.get('application_id', configuration['application_id']),
                                     binding['owner'], binding['space'])
                    target = (ExistingDataset(binding['dataset_id']) if 'dataset_id' in binding
                              else EphemeralCacheSpace(binding['cache_id']))
                    grant = self._registry.grant(binding['service'], REQUIREMENTS[consumer][alias],
                                                 space, consumer, target=target)
                    self._bound[consumer + '.' + alias] = self._registry.bind(grant)
        except BaseException:
            self.close()
            raise

    def _binding(self, name):
        if name not in self._bound:
            raise StorageError('STORAGE_BINDING_UNAVAILABLE', binding=name)
        return self._bound[name]

    @contextmanager
    def _operation(self):
        with self._lock:
            if self._closing:
                raise StorageError('STORAGE_SERVICE_UNAVAILABLE')
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1

    def describe(self, request):
        with self._operation():
            bound = self._binding(request['binding'])
            return {'identity': asdict(bound.identity), 'methods': sorted(bound._methods)}

    def invoke(self, request):
        with self._operation():
            return self._invoke(request)

    def _invoke(self, request):
        from .search import SearchGenerationIncomplete, SearchUnavailable

        bound = self._binding(request['binding'])
        method = request['method']
        if method.startswith('_') or method not in bound._methods or method in {'prepare', 'transaction', 'guard'}:
            raise StorageError('STORAGE_PROCESS_METHOD_REJECTED')
        try:
            result = getattr(bound, method)(*[decode_value(item) for item in request['args']],
                **{key: decode_value(item) for key, item in request['kwargs'].items()})
            return encode_value(result)
        except (SearchUnavailable, FileNotFoundError) as error:
            kind = ('incomplete' if isinstance(error, SearchGenerationIncomplete) else
                    'search' if isinstance(error, SearchUnavailable) else 'missing')
            raise ContractError('STORAGE_OPERATION_FAILED', str(error), status_code=503,
                                details={'kind': kind}) from error

    def relational(self, request):
        try:
            return self._relational(request)
        except DatabaseError as error:
            details = {'kind': 'database'}
            if isinstance(error, CommitUnknown):
                details['operation_id'] = error.operation_id
            raise ContractError(error.code, str(error), status_code=503, details=details) from error

    def _relational(self, request):
        from .knowledge_schema import knowledge_schema
        from .persistence_schema import budget_schema
        from .session_storage import session_schema
        from .storage_structured_wire import PortableStructuredEndpoint

        with self._operation():
            name = request['binding']
            schemas = {'repository.primary': knowledge_schema, 'store.primary': session_schema,
                       'provider.primary': budget_schema}
            if name not in schemas:
                raise StorageError('STORAGE_RELATIONAL_BINDING_REQUIRED')
            with self._lock:
                if name not in self._structured:
                    bound = self._binding(name)
                    self._structured[name] = PortableStructuredEndpoint(bound,
                        schemas=(schemas[name](bound.identity.space.owner_id),))
                endpoint = self._structured[name]
            return endpoint.execute(request['request'])

    def objects(self, request):
        from .storage_object_wire import ObjectTransferEndpoint

        with self._operation():
            name = request['binding']
            with self._lock:
                bound = self._binding(name)
                if not bound._grant.contract.satisfies(OBJECT_V1):
                    raise StorageError('STORAGE_OBJECT_BINDING_REQUIRED')
                if name not in self._objects:
                    self._objects[name] = ObjectTransferEndpoint(bound)
                endpoint = self._objects[name]
            return endpoint.execute(request['request'])

    def close(self):
        with self._lock:
            self._closing = True
            if self._active:
                raise StorageError('STORAGE_SERVICE_BUSY')
            for endpoints in (self._structured, self._objects):
                for name, endpoint in reversed(tuple(endpoints.items())):
                    endpoint.close()
                    del endpoints[name]
            self._registry.close()
            while self._instances:
                self._instances[-1].close()
                self._instances.pop()
            self._bound.clear()


class StorageClient:
    def __init__(self, dependency, binding):
        from .storage_sdk import StorageIdentity

        self._dependency, self._binding = dependency, binding
        metadata = self._call('describe', {'binding': binding})
        identity = metadata['identity']
        self.identity = StorageIdentity(SpaceKey(**identity['space']), identity['dataset_id'])
        self._methods = frozenset(metadata['methods'])

    def _call(self, method, request):
        from .search import SearchGenerationIncomplete, SearchUnavailable

        try:
            return getattr(self._dependency, method)(request)
        except ContractError as error:
            kind = error.details.get('kind')
            if kind == 'database':
                if error.code == 'COMMIT_UNKNOWN':
                    raise CommitUnknown(error.details['operation_id']) from error
                raise DatabaseError(error.code) from error
            if kind in {'search', 'incomplete', 'missing'}:
                types = {'search': SearchUnavailable, 'incomplete': SearchGenerationIncomplete,
                         'missing': FileNotFoundError}
                raise types[kind](str(error)) from error
            raise StorageError(error.code, **error.details) from error

    def relational(self):
        from .storage_structured_wire import PortableStructuredClient

        return PortableStructuredClient(lambda request: self._call('relational',
            {'binding': self._binding, 'request': request}), identity=self.identity)

    def __getattr__(self, method):
        if method.startswith('_') or method not in self._methods:
            raise AttributeError(method)
        if method in {'put', 'read'} and {'reference', 'stat', 'read', 'put'} <= self._methods:
            from .storage_object_wire import ObjectTransferClient

            client = ObjectTransferClient(lambda request: self._call('objects',
                {'binding': self._binding, 'request': request}))
            return getattr(client, method)
        def invoke(*args, **kwargs):
            return decode_value(self._call('invoke', {'binding': self._binding, 'method': method,
                'args': [encode_value(item) for item in args],
                'kwargs': {key: encode_value(item) for key, item in kwargs.items()}}))
        return invoke
