from contextlib import contextmanager
from threading import RLock

from .database_contracts import CommitUnknown, DatabaseError
from .plugin_contracts import ContractError, Method
from .storage_process import decode_value, encode_value
from .storage_protocol import prepare, result_failure, validate_descriptor
from .storage_sdk import EphemeralCacheSpace, ExistingDataset, OBJECT_V1, SpaceKey, StorageError


MAX_INVOKE_NODES = 250000


def storage_methods():
    key = {'type': 'string', 'minLength': 1}
    def request(properties):
        return {'type': 'object', 'properties': properties,
                'required': list(properties), 'additionalProperties': False}
    return {
        'describe': Method(request({'binding': key}), {'type': 'object'}),
        'invoke': Method(request({'binding': key, 'request': {'type': 'object'}}), {'type': 'object'}),
        'relational': Method(request({'binding': key, 'request': {'type': 'object'}}), {'type': 'object'}),
        'objects': Method(request({'binding': key, 'request': {'type': 'object'}}), {'type': 'object'}),
    }


def database_failure(error):
    details = {'kind': 'database'}
    if isinstance(error, CommitUnknown):
        details['operation_id'] = error.operation_id
    return ContractError(error.code, str(error), status_code=503, details=details)


class ApplicationStorage:
    def __init__(self, configuration, *, provider_catalog=None, requirements=None, schemas=None):
        from .plugin_host import PluginSpec
        from .storage_catalog import default_storage_providers
        from .storage_plugins import compile_storage_plugins
        from .storage_sdk import StorageSlot
        from .storage_service import StorageService

        providers = default_storage_providers() if provider_catalog is None else provider_catalog
        consumers = configuration.get('bindings', {})
        if requirements is None:
            requirements = {}
            for consumer, bindings in consumers.items():
                requirements[consumer] = {}
                for alias, binding in bindings.items():
                    service = configuration['services'][binding['service']]
                    instance = configuration['instances'][service['instance']]
                    contracts = providers[instance['provider']].exports[service['export']]
                    candidates = [contract for contract in contracts
                                  if all(contract.satisfies(parent) for parent in contracts)]
                    if len(candidates) != 1:
                        raise ValueError('STORAGE_BINDING_CONTRACT_REQUIRED')
                    requirements[consumer][alias] = candidates[0]
        if set(consumers) - set(requirements):
            raise ValueError('UNKNOWN_APPLICATION_STORAGE_CONSUMER')
        self._schemas = dict(schemas or {})
        specs = tuple(PluginSpec(name, (), tuple(StorageSlot(slot, contract)
            for slot, contract in requirements[name].items()), lambda ctx, config: None)
            for name in consumers)
        compile_storage_plugins({'version': 1, 'plugins': [{'id': name, 'use': name} for name in consumers]},
            specs, configuration, provider_catalog=providers)
        self._registry, self._instances, self._bound, self._structured = StorageService(), [], {}, {}
        self._objects, self._generic = {}, {}
        self._lock, self._active, self._closing = RLock(), 0, False
        try:
            exports = {}
            for name, definition in configuration['instances'].items():
                provider = providers[definition['provider']]
                instance = provider.factory(definition['config'])
                self._instances.append(instance)
                actual = provider.validate_exports(instance)
                exports[name] = actual
            for name, definition in configuration['services'].items():
                self._registry.register(name, exports[definition['instance']][definition['export']])
            for consumer, bindings in consumers.items():
                for alias, binding in bindings.items():
                    space = SpaceKey(binding.get('application_id', configuration['application_id']),
                                     binding['owner'], binding['space'])
                    target = (ExistingDataset(binding['dataset_id']) if 'dataset_id' in binding
                              else EphemeralCacheSpace(binding['cache_id']))
                    grant = self._registry.grant(binding['service'], requirements[consumer][alias],
                                                 space, consumer, target=target)
                    bound = self._registry.bind(grant)
                    self._bound[consumer + '.' + alias] = bound
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
            return bound.describe()

    def invoke(self, request):
        from .storage_json_wire import StorageJsonEndpoint

        with self._operation():
            name = request['binding']
            with self._lock:
                if name not in self._generic:
                    bound = self._binding(name)
                    self._generic[name] = StorageJsonEndpoint(
                        lambda logical: self._invoke(bound, logical), operations=('invoke',),
                        result_error=lambda request, value, error: result_failure(bound.describe(), request['method'], error))
                endpoint = self._generic[name]
            return endpoint.execute(request['request'])

    def _invoke(self, bound, request):
        from .search import SearchGenerationIncomplete, SearchUnavailable

        if (type(request) is not dict or set(request) != {'version', 'op', 'method', 'args', 'kwargs'}
                or type(request['version']) is not int or request['version'] != 1 or request['op'] != 'invoke'
                or type(request['method']) is not str or type(request['args']) is not list
                or type(request['kwargs']) is not dict or len(request['args']) > 64 or len(request['kwargs']) > 64
                or any(type(key) is not str for key in request['kwargs'])):
            raise StorageError('STORAGE_INVALID_PROCESS_REQUEST')
        try:
            return bound._invoke_values(request['method'], request['args'], request['kwargs'],
                max_nodes=MAX_INVOKE_NODES, request_bytes=64 * 1024 * 1024, result_bytes=64 * 1024 * 1024)
        except DatabaseError as error:
            raise database_failure(error) from error
        except (SearchUnavailable, FileNotFoundError) as error:
            kind = ('incomplete' if isinstance(error, SearchGenerationIncomplete) else
                    'search' if isinstance(error, SearchUnavailable) else 'missing')
            raise ContractError('STORAGE_OPERATION_FAILED', str(error), status_code=503,
                                details={'kind': kind}) from error

    def relational(self, request):
        try:
            return self._relational(request)
        except DatabaseError as error:
            raise database_failure(error) from error

    def _relational(self, request):
        from .storage_structured_wire import PortableStructuredEndpoint

        with self._operation():
            name = request['binding']
            if name not in self._schemas:
                raise StorageError('STORAGE_RELATIONAL_BINDING_REQUIRED')
            with self._lock:
                if name not in self._structured:
                    bound = self._binding(name)
                    self._structured[name] = PortableStructuredEndpoint(bound,
                        schemas=(self._schemas[name](bound.identity.space.owner_id),))
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
            for endpoints in (self._generic, self._structured, self._objects):
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
        from .storage_json_wire import StorageJsonClient
        from .storage_sdk import StorageIdentity

        self._dependency, self._binding = dependency, binding
        metadata = validate_descriptor(self._call('describe', {'binding': binding}))
        self._descriptor = metadata
        identity = metadata['identity']
        self.identity = StorageIdentity(SpaceKey(**identity['space']), identity['dataset_id'])
        self._methods = frozenset(metadata['methods'])
        self._values = StorageJsonClient(lambda request: self._call('invoke',
            {'binding': self._binding, 'request': request}), operations=('invoke',))

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
        operation = self._descriptor['operations'].get(method)
        if operation is None or operation['channel'] == 'structured':
            raise AttributeError(method)
        if operation['channel'] == 'object':
            from .storage_object_wire import ObjectTransferClient

            client = ObjectTransferClient(lambda request: self._call('objects',
                {'binding': self._binding, 'request': request}))
            return getattr(client, method)
        def invoke(*args, **kwargs):
            budget = [MAX_INVOKE_NODES]
            request = {'version': 1, 'op': 'invoke', 'method': method,
                'args': [encode_value(item, _budget=budget, _max_bytes=64 * 1024 * 1024) for item in args],
                'kwargs': {key: encode_value(item, _budget=budget, _max_bytes=64 * 1024 * 1024)
                           for key, item in kwargs.items()}}
            with prepare(self._descriptor, method, request['args'], request['kwargs'], max_nodes=MAX_INVOKE_NODES,
                         request_bytes=64 * 1024 * 1024, result_bytes=64 * 1024 * 1024) as call:
                result = self._values.call(request)
                validated = call.complete(lambda: result)
                try:
                    return decode_value(validated, _budget=[MAX_INVOKE_NODES], _validated=True)
                except Exception as error:
                    result_failure(self._descriptor, method, error)
        return invoke
