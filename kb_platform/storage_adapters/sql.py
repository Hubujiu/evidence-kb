from contextlib import contextmanager

from ..database_contracts import DatabaseError, QueryLimits, SchemaPackage, SqlStatement
from ..database_migrations import initialize_space, prepare
from ..storage_sdk import (
    ExistingDataset, RELATIONAL_POSTGRES_V1, RELATIONAL_V1, SpaceKey, StorageError, StorageExport,
)


def relational_contracts(engine):
    return (RELATIONAL_POSTGRES_V1,) if engine == 'postgresql' else (RELATIONAL_V1,)


class SqlStorageProvider:
    def __init__(self, provider, *, adapters=None):
        self._provider = provider
        self._adapters = {} if adapters is None else dict(adapters)
        if 'relational' in self._adapters:
            raise ValueError('STORAGE_RELATIONAL_EXPORT_RESERVED')

    def exports(self):
        exports = [StorageExport('relational', relational_contracts(self._provider.info.engine), self._relational)]
        for name, (contracts, factory) in self._adapters.items():
            exports.append(StorageExport(name, contracts,
                lambda space, target, selected=factory: selected(self._relational(space, target))))
        return tuple(exports)

    def initialize(self, export_name, space):
        if export_name not in {'relational', *self._adapters} or not isinstance(space, SpaceKey):
            raise ValueError('INVALID_STORAGE_INITIALIZATION')
        return initialize_space(self._provider.open_space(space))

    def _relational(self, space, target):
        if not isinstance(target, ExistingDataset):
            raise StorageError('STORAGE_PERSISTENT_DATASET_REQUIRED')
        physical = self._provider.open_space(space)
        initialize_space(physical, expected_dataset_id=target.dataset_id)
        return _SqlSpace(physical)

    def close(self):
        self._provider.close()


class _SqlSpace:
    def __init__(self, space):
        self._space = space
        self._sealed = False
        self.identity, self.info = space.identity, space.info
        self.physical_name = self.schema = space.physical_name
        self.runtime_role = space.runtime_engine.url.username

    def prepare(self, package):
        if self._sealed or not isinstance(package, SchemaPackage):
            raise DatabaseError('DATABASE_PREPARE_CLOSED')
        return prepare(self._space, package)

    def seal(self):
        self._sealed = True
        return self

    @contextmanager
    def transaction(self, *, write=True, mode=None, limits=None, operation_id=None):
        if mode is not None:
            if mode not in {'read', 'write'}:
                raise ValueError('INVALID_DATABASE_TRANSACTION_MODE')
            write = mode == 'write'
        if type(write) is not bool:
            raise ValueError('INVALID_DATABASE_TRANSACTION_MODE')
        self._sealed = True
        with self._space.transaction(write=write, limits=limits, operation_id=operation_id) as tx:
            yield tx

    def batch(self, statements, *, limits=None):
        statements = tuple(statements)
        selected = QueryLimits() if limits is None else limits
        if not statements or len(statements) > selected.max_statements:
            raise DatabaseError('DATABASE_BATCH_LIMIT')
        if any(not isinstance(statement, SqlStatement) for statement in statements):
            raise ValueError('INVALID_DATABASE_BATCH')
        with self.transaction(write=True, limits=selected) as tx:
            return tuple(tx.execute(statement) for statement in statements)

    def query_json(self, request):
        from ..database_process import query_json

        return query_json(self, request)

    def batch_json(self, request):
        from ..database_process import batch_json

        return batch_json(self, request)
