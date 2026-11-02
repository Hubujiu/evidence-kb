from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4

from .implementation_dependencies import dependency_manifest, implementation_dependencies
from .storage_sdk import (
    Contract, EphemeralCacheSpace, ExistingDataset, RELATIONAL_V1, SpaceKey, StorageError,
    StorageExport, StorageIdentity,
)


def _identity(value):
    return tuple(sorted(dict(value).items())) if hasattr(value, 'items') else value


@dataclass(frozen=True)
class StorageGrant:
    service: str
    contract: Contract
    space: SpaceKey
    target: ExistingDataset | EphemeralCacheSpace
    consumer: str
    registration_id: str
    token: str


@dataclass
class _Export:
    export: StorageExport
    registration_id: str
    owner_identity: object
    owner_lease: object
    configuration_sha256: str | None
    active: int = 0


class StorageService:
    def __init__(self):
        self._exports, self._grants, self._issued = {}, {}, set()
        self._lock = RLock()
        self._closed = False

    def register(self, name, export, *, owner_identity=None, owner_lease=None, registration_id=None,
                 configuration_sha256=None):
        if (not isinstance(name, str) or not name or not isinstance(export, StorageExport)
                or configuration_sha256 is not None and
                (not isinstance(configuration_sha256, str) or re.fullmatch('[0-9a-f]{64}', configuration_sha256) is None)):
            raise ValueError('INVALID_STORAGE_REGISTRATION')
        registration = uuid4().hex if registration_id is None else str(registration_id)
        owner = _identity(owner_identity)
        with self._lock:
            if self._closed or name in self._exports:
                raise StorageError('STORAGE_SERVICE_UNAVAILABLE', service=name)
            if (owner, registration) in self._issued:
                raise StorageError('STORAGE_REGISTRATION_REUSED', service=name)
            entry = _Export(export, registration, owner, owner_lease, configuration_sha256)
            self._exports[name] = entry
            self._issued.add((owner, registration))
        return lambda: self._unregister(name, entry)

    def _entry(self, name, registration=None):
        entry = self._exports.get(name)
        if self._closed or entry is None:
            raise StorageError('STORAGE_SERVICE_UNAVAILABLE', service=name)
        if registration is not None and entry.registration_id != registration:
            raise StorageError('STORAGE_BINDING_CHANGED', service=name)
        return entry

    def grant(self, name, contract, space, consumer, *, target):
        if (not isinstance(contract, Contract) or not isinstance(space, SpaceKey)
                or not isinstance(consumer, str) or not consumer
                or not isinstance(target, (ExistingDataset, EphemeralCacheSpace))):
            raise ValueError('INVALID_STORAGE_GRANT')
        with self._lock:
            entry = self._entry(name)
            if not any(actual.satisfies(contract) for actual in entry.export.contracts):
                raise StorageError('STORAGE_CONTRACT_MISSING', service=name, consumer=consumer,
                                   required=contract.key,
                                   actual=tuple(item.key for item in entry.export.contracts))
            grant = StorageGrant(name, contract, space, target, consumer, entry.registration_id, uuid4().hex)
            self._grants[grant.token] = grant
            return grant

    def bind(self, grant, *, owner_lease=None):
        with self._lock:
            if not isinstance(grant, StorageGrant) or self._grants.get(grant.token) is not grant:
                raise StorageError('STORAGE_GRANT_REJECTED')
        try:
            with self.lease(grant.service, grant.registration_id) as export:
                implementation = export.open_space(grant.space, grant.target)
                for contract in export.contracts:
                    contract.validate(implementation)
                    if 'dependency_manifest' in _methods(contract):
                        try:
                            dependency_manifest(implementation.dependency_manifest())
                        except (TypeError, ValueError) as error:
                            raise StorageError('STORAGE_IMPLEMENTATION_INVALID', contract=contract.key) from error
                actual = getattr(implementation, 'identity', None)
                expected = (grant.target.dataset_id if isinstance(grant.target, ExistingDataset)
                            else grant.target.cache_id)
                if actual != StorageIdentity(grant.space, expected):
                    raise StorageError('STORAGE_DATASET_IDENTITY_MISMATCH')
        except StorageError:
            raise
        except Exception as error:
            code = getattr(error, 'code', 'STORAGE_BACKEND_UNAVAILABLE')
            raise StorageError(code, service=grant.service, consumer=grant.consumer) from None
        return BoundStorage(self, grant, implementation, owner_lease=owner_lease)

    @contextmanager
    def lease(self, name, registration):
        with self._lock:
            entry = self._entry(name, registration)
            ownership = entry.owner_lease() if entry.owner_lease is not None else nullcontext()
        with ownership:
            with self._lock:
                entry = self._entry(name, registration)
                entry.active += 1
            try:
                yield entry.export
            finally:
                with self._lock:
                    entry.active -= 1

    def catalog(self):
        with self._lock:
            return {name: tuple(contract.key for contract in entry.export.contracts)
                    for name, entry in self._exports.items()}

    def inspect(self):
        with self._lock:
            return {name: {'registration_id': entry.registration_id,
                           'owner_identity': entry.owner_identity, 'active_calls': entry.active}
                    for name, entry in self._exports.items()}

    def _unregister(self, name, entry):
        with self._lock:
            if self._exports.get(name) is not entry:
                return
            if entry.active:
                raise StorageError('STORAGE_SERVICE_BUSY', service=name)
            del self._exports[name]
            for token, grant in tuple(self._grants.items()):
                if grant.service == name and grant.registration_id == entry.registration_id:
                    del self._grants[token]

    def close(self):
        with self._lock:
            if any(entry.active for entry in self._exports.values()):
                raise StorageError('STORAGE_SERVICE_BUSY')
            self._exports.clear()
            self._grants.clear()
            self._closed = True


def _methods(contract):
    methods = set().union(*(_methods(parent) for parent in contract.extends)) if contract.extends else set()
    if contract.interface is not None:
        methods.update(name for cls in contract.interface.__mro__ for name, value in vars(cls).items()
                       if not name.startswith('_') and callable(value))
    return methods


class BoundStorage:
    def __init__(self, service, grant, implementation, *, owner_lease=None, consumer_lease=None):
        self._service, self._grant, self._implementation = service, grant, implementation
        self._owner_lease, self._consumer_lease = owner_lease, consumer_lease
        self._methods = _methods(grant.contract)
        self.identity = implementation.identity
        self.registration_id = grant.registration_id
        from .storage_protocol import validate_binding

        self._descriptor = validate_binding(self)

    def for_consumer(self, identity, lease, *, consumer_name):
        if consumer_name != self._grant.consumer:
            raise StorageError('STORAGE_CONSUMER_GRANT_MISMATCH')
        bound = BoundStorage(self._service, self._grant, self._implementation,
                             owner_lease=self._owner_lease, consumer_lease=lease)
        bound._consumer_identity = _identity(identity)
        return bound

    def call_json(self, request):
        from .storage_process import call_json

        return call_json(self, request)

    def _dependency_manifest(self):
        with self._lease():
            try:
                implementation = dependency_manifest(self._implementation.dependency_manifest())
            except (TypeError, ValueError) as error:
                raise StorageError('STORAGE_DEPENDENCY_MANIFEST_INVALID') from error
            with self._service._lock:
                configured = self._service._entry(self._grant.service, self._grant.registration_id).configuration_sha256
            return {'version': 1, 'implementation': implementation_dependencies(__file__,
                    Path(__file__).with_name('storage_plugins.py'),
                    Path(__file__).parent / 'storage_sdk/__init__.py'),
                'storage': {'identity': asdict(self.identity), 'contract': self._grant.contract.key},
                'configuration_sha256': configured, 'provider': implementation}

    @contextmanager
    def _lease(self, *, include_consumer=True):
        consumer = self._consumer_lease() if include_consumer and self._consumer_lease is not None else nullcontext()
        owner = self._owner_lease() if self._owner_lease is not None else nullcontext()
        with consumer, owner, self._service.lease(self._grant.service, self._grant.registration_id):
            yield

    @contextmanager
    def _context(self, context):
        with self._lease(), context as value:
            yield self if value is self._implementation else value

    def describe(self):
        return deepcopy(self._descriptor)

    def _execute(self, name, args, kwargs, encode, *, max_nodes, request_bytes, result_bytes, invoke):
        from .storage_protocol import prepare

        with self._lease():
            with prepare(self._descriptor, name, args, kwargs, max_nodes=max_nodes,
                         request_bytes=request_bytes, result_bytes=result_bytes) as call:
                result = invoke()
                encoded = call.complete(lambda: encode(result))
                return result, encoded

    def _invoke_values(self, name, args, kwargs, *, max_nodes, request_bytes, result_bytes):
        from .storage_process import decode_value, encode_value

        def invoke():
            budget = [max_nodes]
            decoded_args = [decode_value(value, _budget=budget, _validated=True) for value in args]
            decoded_kwargs = {key: decode_value(value, _budget=budget, _validated=True) for key, value in kwargs.items()}
            callback = self._dependency_manifest if name == 'dependency_manifest' else getattr(self._implementation, name)
            return callback(*decoded_args, **decoded_kwargs)

        return self._execute(name, args, kwargs,
            lambda result: encode_value(result, _budget=[max_nodes], _max_bytes=result_bytes),
            max_nodes=max_nodes, request_bytes=request_bytes, result_bytes=result_bytes, invoke=invoke)[1]

    def __getattr__(self, name):
        if name in {'info', 'schema', 'physical_name', 'runtime_role'} and self._grant.contract.satisfies(RELATIONAL_V1):
            return getattr(self._implementation, name)
        if name not in self._methods:
            raise AttributeError(name)
        operation = self._descriptor['operations'].get(name)

        def invoke(*args, **kwargs):
            if operation is not None and operation['channel'] != 'structured':
                from .storage_process import encode_value
                from .storage_protocol import MAX_VALUE_BYTES, MAX_VALUE_NODES

                budget = [MAX_VALUE_NODES]
                encoded_args = [encode_value(value, _budget=budget, _max_bytes=MAX_VALUE_BYTES) for value in args]
                encoded_kwargs = {key: encode_value(value, _budget=budget, _max_bytes=MAX_VALUE_BYTES)
                                  for key, value in kwargs.items()}
                callback = self._dependency_manifest if name == 'dependency_manifest' else getattr(self._implementation, name)
                return self._execute(name, encoded_args, encoded_kwargs,
                    lambda result: encode_value(result, _budget=[MAX_VALUE_NODES], _max_bytes=MAX_VALUE_BYTES),
                    max_nodes=MAX_VALUE_NODES, request_bytes=MAX_VALUE_BYTES, result_bytes=MAX_VALUE_BYTES,
                    invoke=lambda: callback(*args, **kwargs))[0]
            with self._lease(include_consumer=name not in {'prepare', 'seal'}):
                result = getattr(self._implementation, name)(*args, **kwargs)
                if result is self._implementation:
                    return self
                if hasattr(result, '__enter__') and hasattr(result, '__exit__'):
                    return self._context(result)
                return result

        return invoke
