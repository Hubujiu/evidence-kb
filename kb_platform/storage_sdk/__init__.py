from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
import re
from typing import Generic, Protocol, TypeVar

from ..database_contracts import QueryLimits, SchemaPackage, SpaceKey, StorageIdentity


T = TypeVar('T')


class StorageError(RuntimeError):
    def __init__(self, code, **details):
        self.code = code
        self.details = details
        super().__init__(code)


@dataclass(frozen=True)
class Contract(Generic[T]):
    id: str
    major: int
    extends: tuple['Contract', ...] = ()
    interface: type[T] | None = None

    def __post_init__(self):
        if (not isinstance(self.id, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]*', self.id)
                or type(self.major) is not int or self.major < 1):
            raise ValueError('INVALID_STORAGE_CONTRACT')
        object.__setattr__(self, 'extends', tuple(self.extends))
        if any(not isinstance(parent, Contract) or parent.key == self.key for parent in self.extends):
            raise ValueError('INVALID_STORAGE_CONTRACT_PARENT')

    @property
    def key(self):
        return self.id + '/v' + str(self.major)

    @property
    def category(self):
        return self.id.split('.')[1] if self.id.startswith('storage.') else self.id.split('.')[0]

    def satisfies(self, required):
        return self == required or any(parent.satisfies(required) for parent in self.extends)

    def validate(self, implementation):
        for parent in self.extends:
            parent.validate(implementation)
        if self.interface is not None:
            methods = {name for cls in self.interface.__mro__ for name, value in vars(cls).items()
                       if not name.startswith('_') and callable(value)}
            if any(not callable(getattr(implementation, name, None)) for name in methods):
                raise StorageError('STORAGE_IMPLEMENTATION_INVALID', contract=self.key)


@dataclass(frozen=True)
class StorageSlot(Generic[T]):
    name: str
    contract: Contract[T]

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or not isinstance(self.contract, Contract):
            raise ValueError('INVALID_STORAGE_SLOT')


@dataclass(frozen=True)
class ExistingDataset:
    dataset_id: str

    def __post_init__(self):
        if not isinstance(self.dataset_id, str) or not self.dataset_id or len(self.dataset_id) > 200:
            raise ValueError('STORAGE_DATASET_ID_REQUIRED')


@dataclass(frozen=True)
class EphemeralCacheSpace:
    cache_id: str

    def __post_init__(self):
        if not isinstance(self.cache_id, str) or not self.cache_id or len(self.cache_id) > 200:
            raise ValueError('STORAGE_CACHE_ID_REQUIRED')


StorageTarget = ExistingDataset | EphemeralCacheSpace


@dataclass(frozen=True)
class StorageExport(Generic[T]):
    name: str
    contracts: tuple[Contract, ...]
    open_space: Callable[[SpaceKey, StorageTarget], T]

    def __post_init__(self):
        object.__setattr__(self, 'contracts', tuple(self.contracts))
        if (not isinstance(self.name, str) or not self.name or not self.contracts
                or any(not isinstance(contract, Contract) for contract in self.contracts)
                or len({contract.key for contract in self.contracts}) != len(self.contracts)
                or not callable(self.open_space)):
            raise ValueError('INVALID_STORAGE_EXPORT')


@dataclass(frozen=True)
class Document:
    id: str
    value: dict
    revision: int


class RelationalStore(Protocol):
    identity: StorageIdentity

    def prepare(self, package: SchemaPackage): ...
    def seal(self): ...
    def transaction(self, *, write: bool = True, mode=None, limits: QueryLimits | None = None,
                    operation_id=None) -> AbstractContextManager: ...
    def batch(self, statements, *, limits=None): ...
    def query_json(self, request): ...
    def batch_json(self, request): ...


class DocumentStore(Protocol):
    identity: StorageIdentity

    def get(self, document_id: str) -> Document | None: ...
    def get_many(self, ids: tuple[str, ...]) -> tuple[Document | None, ...]: ...
    def put(self, document_id: str, value: dict, *, expected_revision: int | None = None) -> Document: ...
    def delete(self, document_id: str, *, expected_revision: int | None = None) -> bool: ...


class KeyValueStore(Protocol):
    identity: StorageIdentity

    def get(self, key: str) -> bytes | None: ...
    def set(self, key: str, value: bytes) -> None: ...
    def delete(self, key: str) -> bool: ...


class ExpiringKeyValueStore(KeyValueStore, Protocol):
    def put_expiring(self, key: str, value: bytes, *, ttl_seconds: float) -> None: ...


class VectorStore(Protocol):
    identity: StorageIdentity

    def stage(self, index: str, records, vector_dimension=None): ...
    def verify(self, index: str) -> dict: ...
    def seal(self, index: str, checksum: str): ...
    def is_sealed(self, index: str, checksum: str) -> bool: ...
    def read_records(self, index: str, ids=None): ...
    def query(self, index: str, query, limit: int, *, filters: dict | None = None) -> list[dict]: ...


class LexicalStore(Protocol):
    identity: StorageIdentity

    def stage(self, index: str, records, vector_dimension=None): ...
    def verify(self, index: str) -> dict: ...
    def seal(self, index: str, checksum: str): ...
    def is_sealed(self, index: str, checksum: str) -> bool: ...
    def read_records(self, index: str, ids=None): ...
    def query(self, index: str, query: str, limit: int, *, filters: dict | None = None) -> list[dict]: ...


class ObjectStore(Protocol):
    identity: StorageIdentity

    def put(self, tenant_id: str, data: bytes): ...
    def reference(self, tenant_id: str, locator: str, *, sha256=None): ...
    def read(self, tenant_id: str, locator: str, *, sha256=None) -> bytes: ...
    def stat(self, tenant_id: str, locator: str, *, sha256=None): ...


RELATIONAL_V1 = Contract('storage.relational', 1, interface=RelationalStore)
RELATIONAL_POSTGRES_V1 = Contract('storage.relational.postgres', 1, (RELATIONAL_V1,))
DOCUMENT_V1 = Contract('storage.document', 1, interface=DocumentStore)
KV_V1 = Contract('storage.kv', 1, interface=KeyValueStore)
KV_EXPIRING_V1 = Contract('storage.kv.expiring', 1, (KV_V1,), ExpiringKeyValueStore)
KV_DURABLE_V1 = Contract('storage.kv.durable', 1, (KV_V1,))
VECTOR_V1 = Contract('storage.vector', 1, interface=VectorStore)
LEXICAL_V1 = Contract('storage.lexical', 1, interface=LexicalStore)
LEXICAL_BM25_V1 = Contract('storage.lexical.bm25', 1, (LEXICAL_V1,))
OBJECT_V1 = Contract('storage.object', 1, interface=ObjectStore)


@dataclass(frozen=True)
class StorageProvider:
    factory: Callable[[dict], object]
    exports: Mapping[str, tuple[Contract, ...]]

    def __post_init__(self):
        from types import MappingProxyType

        if not callable(self.factory) or not isinstance(self.exports, Mapping) or not self.exports:
            raise ValueError('INVALID_STORAGE_PROVIDER')
        exports = {name: tuple(contracts) for name, contracts in self.exports.items()}
        if any(not isinstance(name, str) or not name or not contracts
               or any(not isinstance(contract, Contract) for contract in contracts)
               for name, contracts in exports.items()):
            raise ValueError('INVALID_STORAGE_PROVIDER')
        object.__setattr__(self, 'exports', MappingProxyType(exports))
