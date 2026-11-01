import json
import os
from pathlib import Path
from uuid import uuid4

from .database_providers import MySQLProvider, PostgresProvider, SQLiteProvider
from .storage_adapters.objects import file_objects, s3_objects
from .storage_adapters.redis_cache import RedisCacheProvider
from .storage_adapters.sql import SqlStorageProvider, relational_contracts
from .storage_adapters.values import SqlDocumentStore, SqlKeyValueStore
from .storage_sdk import (
    DOCUMENT_V1, ExistingDataset, KV_DURABLE_V1, KV_V1, LEXICAL_BM25_V1, OBJECT_V1,
    StorageError, StorageExport, StorageIdentity, StorageProvider, VECTOR_V1, KV_EXPIRING_V1,
)


def resolve_inputs(config):
    result = {}
    for key, value in config.items():
        if key.endswith('_env'):
            name = key[:-4]
            if name in config or not isinstance(value, str) or not os.environ.get(value):
                raise StorageError('STORAGE_ENVIRONMENT_REQUIRED', input=key)
            result[name] = os.environ[value]
        else:
            result[key] = value
    return result


def _search(database, kind):
    from .knowledge_storage import knowledge_schema
    from .postgres_search import PostgresSearch
    from .search_channels import SearchLexicalAdapter, SearchVectorAdapter

    database.prepare(knowledge_schema(database.identity.space.owner_id))
    database.seal()
    backend = PostgresSearch(database=database)
    adapter = SearchVectorAdapter if kind == 'vector' else SearchLexicalAdapter
    return adapter(backend, identity=database.identity)


def _sql(config, engine):
    options = resolve_inputs(config)
    if engine == 'sqlite':
        if set(options) != {'root'}:
            raise ValueError('INVALID_SQLITE_STORAGE_CONFIGURATION')
        provider = SQLiteProvider(options['root'])
    else:
        provider = (PostgresProvider if engine == 'postgresql' else MySQLProvider)(**options)
    adapters = {'kv': ((KV_V1, KV_DURABLE_V1), SqlKeyValueStore)}
    if engine != 'mysql':
        adapters['document'] = ((DOCUMENT_V1,), SqlDocumentStore)
    if engine == 'postgresql':
        adapters.update(vector=((VECTOR_V1,), lambda db: _search(db, 'vector')),
                        lexical=((LEXICAL_BM25_V1,), lambda db: _search(db, 'lexical')))
    return SqlStorageProvider(provider, adapters=adapters)


class EmbeddedStorageProvider:
    def __init__(self, config):
        selected = resolve_inputs(config)
        if set(selected) != {'root'}:
            raise ValueError('INVALID_EMBEDDED_STORAGE_CONFIGURATION')
        self.root, self._backends = Path(selected['root']).resolve(), {}

    def _identity(self, space):
        path = self.root / space.digest / 'storage-identity.json'
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text('utf-8'))
            if set(value) != {'space', 'dataset_id'} or value['space'] != space.key or not value['dataset_id']:
                raise ValueError('identity')
            return StorageIdentity(space, value['dataset_id'])
        except (ValueError, TypeError):
            raise StorageError('STORAGE_DATASET_IDENTITY_CORRUPT') from None

    def initialize(self, export_name, space):
        if export_name not in {'vector', 'lexical'}:
            raise ValueError('INVALID_STORAGE_INITIALIZATION')
        existing = self._identity(space)
        if existing is not None:
            return existing
        identity = StorageIdentity(space, uuid4().hex)
        path = self.root / space.digest / 'storage-identity.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / (uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump({'space': space.key, 'dataset_id': identity.dataset_id}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return self._identity(space)

    def _open(self, kind, space, target):
        from .embedded_search import EmbeddedSearch
        from .search_channels import SearchLexicalAdapter, SearchVectorAdapter

        if not isinstance(target, ExistingDataset) or self._identity(space) != StorageIdentity(space, target.dataset_id):
            raise StorageError('STORAGE_DATASET_IDENTITY_MISMATCH')
        if space not in self._backends:
            self._backends[space] = EmbeddedSearch(self.root / space.digest / 'indexes')
        adapter = SearchVectorAdapter if kind == 'vector' else SearchLexicalAdapter
        return adapter(self._backends[space], identity=StorageIdentity(space, target.dataset_id))

    def exports(self):
        return (StorageExport('vector', (VECTOR_V1,), lambda space, target: self._open('vector', space, target)),
                StorageExport('lexical', (LEXICAL_BM25_V1,), lambda space, target: self._open('lexical', space, target)))

    def close(self):
        for backend in self._backends.values():
            backend.close()
        self._backends.clear()


def default_storage_providers():
    result = {}
    for engine in ('postgresql', 'mysql', 'sqlite'):
        exports = {'relational': relational_contracts(engine), 'kv': (KV_V1, KV_DURABLE_V1)}
        if engine != 'mysql':
            exports['document'] = (DOCUMENT_V1,)
        if engine == 'postgresql':
            exports.update(vector=(VECTOR_V1,), lexical=(LEXICAL_BM25_V1,))
        result[engine] = StorageProvider(lambda config, selected=engine: _sql(config, selected), exports)
    result['redis'] = StorageProvider(lambda config: RedisCacheProvider(**resolve_inputs(config)),
                                      {'kv': (KV_V1, KV_EXPIRING_V1)})
    result['files'] = StorageProvider(lambda config: file_objects(resolve_inputs(config)), {'object': (OBJECT_V1,)})
    result['s3'] = StorageProvider(lambda config: s3_objects(resolve_inputs(config)), {'object': (OBJECT_V1,)})
    result['embedded'] = StorageProvider(EmbeddedStorageProvider,
                                        {'vector': (VECTOR_V1,), 'lexical': (LEXICAL_BM25_V1,)})
    return result


def initialize_configuration(configuration, *, provider_catalog=None):
    from copy import deepcopy
    from .storage_sdk import EphemeralCacheSpace, SpaceKey

    selected = deepcopy(configuration)
    if selected.get('version') != 2:
        raise ValueError('INVALID_STORAGE_CONFIGURATION')
    catalog = default_storage_providers() if provider_catalog is None else provider_catalog
    instances, seen = {}, {}
    try:
        for consumer, slots in selected['bindings'].items():
            for binding in slots.values():
                if 'cache_id' in binding:
                    if 'dataset_id' in binding:
                        raise ValueError('INVALID_STORAGE_BINDING')
                    EphemeralCacheSpace(binding['cache_id'])
                    continue
                definition = selected['services'][binding['service']]
                name, export = definition['instance'], definition['export']
                if name not in instances:
                    row = selected['instances'][name]
                    instances[name] = catalog[row['provider']].factory(row['config'])
                space = SpaceKey(binding.get('application_id', selected['application_id']),
                                 binding['owner'], binding['space'])
                existing = binding.get('dataset_id')
                if existing is not None:
                    exports = {item.name: item for item in instances[name].exports()}
                    identity = exports[export].open_space(space, ExistingDataset(existing)).identity
                else:
                    identity = instances[name].initialize(export, space)
                if space in seen and seen[space] != (name, identity.dataset_id):
                    raise ValueError('STORAGE_SPACE_SERVICE_CONFLICT')
                seen[space] = (name, identity.dataset_id)
                binding['dataset_id'] = identity.dataset_id
        return selected
    finally:
        for instance in reversed(tuple(instances.values())):
            instance.close()
