from pathlib import Path
from threading import Lock

from .plugin_host import PluginSpec
from .plugin_configuration import configured_plugins, config_schema, TEXT, POSITIVE, file_inputs


class PostgresKnowledgeRepository:
    def __init__(self, dsn):
        self._dsn, self._service, self._repository = dsn, None, None
        self._lock, self._closed = Lock(), False

    @property
    def database(self):
        return self._repository.database if self._repository is not None else None

    @property
    def identity(self):
        return self._get().identity

    def _get(self):
        from .database_providers.postgres import PostgresProvider
        from .database_service import DatabaseService
        from .knowledge_storage import DEFAULT_SPACE, KnowledgeRepository

        with self._lock:
            if self._closed:
                raise ValueError('KNOWLEDGE_REPOSITORY_CLOSED')
            if self._repository is None:
                service = DatabaseService()
                try:
                    service.register('knowledge', PostgresProvider(self._dsn, provision=False, schema='public'))
                    database = service.bind(service.grant('knowledge', DEFAULT_SPACE, 'knowledge-repository'))
                    database.seal()
                    self._repository = KnowledgeRepository(database=database, prepare=False)
                except BaseException:
                    service.close()
                    raise
                self._service = service
            return self._repository

    def transaction(self, actor, kb_id=None, *, write=True):
        return self._get().transaction(actor, kb_id, write=write)

    def job_transaction(self, tenant_id, kb_id, *, write=True):
        return self._get().job_transaction(tenant_id, kb_id, write=write)

    def probe(self):
        return self._get().probe()

    def close(self):
        with self._lock:
            if self._service is not None:
                self._service.close()
                self._service = None
            self._closed = True


class OwnedModelClients:
    def __init__(self, generation, embedding):
        self.values = generation, embedding
        self._unclaimed = list(dict((id(value), value) for value in self.values).values())
        self._claim_started = False

    def claim(self, context):
        if self._claim_started:
            raise ValueError('MODEL_CLIENT_OWNERSHIP_ALREADY_CLAIMED')
        self._claim_started = True
        while self._unclaimed:
            client = self._unclaimed[0]
            context.defer(client.close)
            self._unclaimed.pop(0)
        return self.values

    def close_unclaimed(self):
        while self._unclaimed:
            self._unclaimed[-1].close()
            self._unclaimed.pop()




def platform_plugins(settings, *, platform=None, research=None, model_clients=None):
    def clients(ctx, config):
        from .remote import GoogleEmbeddingProvider, LunaClient

        if model_clients is None:
            selected = config['providers_config']
            embedding = GoogleEmbeddingProvider(selected)
            ctx.defer(embedding.close)
            generation = LunaClient(config.get('generation_config', selected))
            ctx.defer(generation.close)
            values = {'generation': generation, 'embedding': embedding}
        else:
            generation, embedding = model_clients.claim(ctx) if isinstance(model_clients, OwnedModelClients) else model_clients
            values = {'generation': generation, 'embedding': embedding}
        ctx.provide('model.clients', values)

    def embedding_client(ctx, config):
        from .model_configuration import EmbeddingView

        client = ctx.require('model.clients')[config.get('client', 'embedding')]
        ctx.provide('embedding', EmbeddingView(ctx.require('models'), config.get('route', 'embedding'),
                    dimension=client.dimension, manifest=client.manifest))

    def ocr_client(ctx, config):
        from .model_configuration import OCRView

        client = ctx.require('model.clients')[config.get('client', 'generation')]
        ctx.provide('ocr', OCRView(ctx.require('models'), config.get('route', 'generation'),
                    manifest=client.manifest, stats=client.snapshot_stats))

    def repository(ctx, config):
        from .platform_ports import KnowledgeRepository

        if 'database_slot' in config:
            from .storage_plugins import require_storage

            value = KnowledgeRepository(database=require_storage(ctx, config['database_slot']))
        else:
            value = PostgresKnowledgeRepository(config.get('dsn', settings.dsn))
        ctx.defer(value.close)
        ctx.provide('platform.repository', value)

    def originals(ctx, config):
        from .storage import OriginalStore

        value = OriginalStore(config.get('root', settings.storage_root))
        ctx.defer(value.close)
        ctx.provide('platform.originals', value)

    def bound_originals(ctx, config):
        from .storage import OriginalStore
        from .storage_plugins import require_storage

        ctx.provide('platform.originals', OriginalStore(bound=require_storage(ctx, 'objects')))

    def parser(ctx, config):
        from .document_parser import parse

        ctx.provide('platform.parser', parse)

    def jobs(ctx, config):
        from .embedding_jobs import EmbeddingJobs

        ctx.provide('platform.embedding_jobs', EmbeddingJobs(ctx.require('platform.repository'), **config))

    def postgres_channel(service_name, *, repository_fallback=False):
        def mount(ctx, config):
            from .postgres_search import PostgresSearch

            if 'database_slot' in config:
                from .storage_plugins import require_storage
                from .knowledge_storage import knowledge_schema

                database = require_storage(ctx, config['database_slot'])
                database.prepare(knowledge_schema(database.identity.space.owner_id))
                database.seal()
                service = PostgresSearch(database=database)
            elif repository_fallback and 'dsn' not in config:
                selected = ctx.require('platform.repository')
                service = (PostgresSearch(database=selected.database) if selected.database is not None
                           else PostgresSearch(config.get('dsn', settings.dsn)))
            else:
                service = PostgresSearch(config.get('dsn', settings.dsn))
            ctx.defer(service.close)
            if service_name in ('platform.search.bm25', 'platform.search.dense'):
                from .search_channels import SearchLexicalAdapter, SearchVectorAdapter

                adapter = SearchLexicalAdapter if service_name.endswith('bm25') else SearchVectorAdapter
                service = adapter(service, identity=database.identity if 'database_slot' in config else None)
            ctx.provide(service_name, service)
        return mount

    def embedded_channel(service_name):
        def mount(ctx, config):
            from .embedded_search import EmbeddedSearch

            service = EmbeddedSearch(config.get('root', Path(settings.storage_root) / '.search-indexes'))
            ctx.defer(service.close)
            if service_name in ('platform.search.bm25', 'platform.search.dense'):
                from .search_channels import SearchLexicalAdapter, SearchVectorAdapter

                adapter = SearchLexicalAdapter if service_name.endswith('bm25') else SearchVectorAdapter
                service = adapter(service)
            ctx.provide(service_name, service)
        return mount

    def split(ctx, config):
        from .search_router import SplitSearch

        ctx.provide('platform.search', SplitSearch(ctx.require('platform.search.bm25'),
                                                   ctx.require('platform.search.dense'),
                                                   ctx.require('platform.search.source')))

    def stored_search(ctx, config):
        from .search_router import SplitSearch
        from .search_evidence import DocumentEvidenceSource
        from .storage_plugins import require_storage

        ctx.provide('platform.search', SplitSearch(require_storage(ctx, 'bm25'),
            require_storage(ctx, 'dense'), DocumentEvidenceSource(require_storage(ctx, 'source'))))

    def opensearch(ctx, config):
        from .search import OpenSearch

        ctx.provide('platform.search', OpenSearch(config.get('url', settings.search_url)))

    def capabilities(*names):
        def mount(ctx, config):
            for name in ('embedding', 'ocr'):
                ctx.provide('platform.' + name, ctx.require(name) if name in names else None)
        return mount

    def service(ctx, config):
        from .core import Platform

        ctx.provide('platform', Platform(
            repository=ctx.require('platform.repository'), search=ctx.require('platform.search'),
            store=ctx.require('platform.originals'), parser=ctx.require('platform.parser'),
            embedding_jobs=ctx.require('platform.embedding_jobs'),
            embedding_provider=ctx.require('platform.embedding'), ocr_client=ctx.require('platform.ocr')))

    def borrowed(ctx, config):
        if platform is None:
            raise ValueError('PLATFORM_REQUIRED')
        ctx.provide('platform', platform)

    def borrowed_research(ctx, config):
        ctx.provide('research', research)

    def research_service(ctx, config):
        from .model_configuration import TextView
        from .research import ResearchService

        model = ctx.require('knowledge.model')
        text = TextView(ctx.require('models'), model, config.get('route', 'generation'))
        ctx.provide('research', ResearchService(ctx.require('platform'), text,
                    ctx.require('platform.embedding'), root=config.get('root', '.local/research-v4e-pruned')))

    def client_inputs(config):
        from .remote import read_provider_config

        if model_clients is not None:
            return {}
        paths = [config['providers_config'], config.get('generation_config', config['providers_config'])]
        _, selected = read_provider_config(paths[0], 'google', credentials=True)
        read_provider_config(paths[1], 'luna')
        if selected.get('platform') == 'vertex_ai':
            paths.append(selected['service_account_file'])
        return file_inputs(paths)

    schemas = {
        'platform.model_clients.remote': config_schema(
            {'providers_config': TEXT, 'generation_config': TEXT}, required=('providers_config',)),
        'platform.embedding.client': config_schema({'route': TEXT, 'client': TEXT}),
        'platform.ocr.client': config_schema({'route': TEXT, 'client': TEXT}),
        'platform.repository.postgres': config_schema({'dsn': {'type': 'string'}, 'database_slot': TEXT}),
        'platform.repository.storage': config_schema({'database_slot': {'const': 'primary'}},
                                                    required=('database_slot',)),
        'platform.originals.files': config_schema({'root': TEXT}),
        'platform.embedding_jobs': config_schema({key: POSITIVE for key in
            ('lease_seconds', 'max_attempts', 'circuit_threshold', 'circuit_seconds')}),
        **{key: config_schema({'dsn': {'type': 'string'}, 'database_slot': TEXT}) for key in
           ('platform.search.postgres', 'platform.search.bm25.postgres', 'platform.search.dense.postgres')},
        **{key: config_schema({'root': TEXT}) for key in
           ('platform.search.embedded', 'platform.search.bm25.embedded', 'platform.search.dense.embedded')},
        'platform.search.opensearch': config_schema({'url': {'type': 'string', 'pattern': '^https?://'}}),
        'platform.research': config_schema({'root': TEXT, 'route': TEXT}),
    }
    from .storage_sdk import DOCUMENT_V1, LEXICAL_BM25_V1, OBJECT_V1, RELATIONAL_V1, StorageSlot, VECTOR_V1

    schemas['platform.search.source.postgres'] = schemas['platform.repository.postgres']
    checks = {'platform.model_clients.remote': lambda config: client_inputs(config)}
    return configured_plugins((
        PluginSpec('platform.model_clients.remote', ('model.clients',), (), clients),
        PluginSpec('platform.embedding.client', ('embedding',), ('models', 'model.clients', 'models.route.embedding'), embedding_client),
        PluginSpec('platform.ocr.client', ('ocr',), ('models', 'model.clients', 'models.route.generation'), ocr_client),
        PluginSpec('platform.repository.postgres', ('platform.repository',), (), repository),
        PluginSpec('platform.repository.storage', ('platform.repository',), (), repository,
                   storage_slots=(StorageSlot('primary', RELATIONAL_V1),)),
        PluginSpec('platform.originals.files', ('platform.originals',), (), originals),
        PluginSpec('platform.originals.storage', ('platform.originals',), (), bound_originals,
                   storage_slots=(StorageSlot('objects', OBJECT_V1),)),
        PluginSpec('platform.parser.native', ('platform.parser',), (), parser),
        PluginSpec('platform.embedding_jobs', ('platform.embedding_jobs',), ('platform.repository',), jobs),
        PluginSpec('platform.search.postgres', ('platform.search',), ('platform.repository',),
                   postgres_channel('platform.search', repository_fallback=True)),
        PluginSpec('platform.search.embedded', ('platform.search',), (), embedded_channel('platform.search')),
        PluginSpec('platform.search.source.postgres', ('platform.search.source',), ('platform.repository',),
                   postgres_channel('platform.search.source', repository_fallback=True)),
        *(PluginSpec('platform.search.' + channel + '.' + backend, ('platform.search.' + channel,), (),
                     postgres_channel('platform.search.' + channel) if backend == 'postgres'
                     else embedded_channel('platform.search.' + channel))
          for channel in ('bm25', 'dense') for backend in ('postgres', 'embedded')),
        PluginSpec('platform.search.split', ('platform.search',),
                   ('platform.search.bm25', 'platform.search.dense', 'platform.search.source'), split),
        PluginSpec('platform.search.storage', ('platform.search',), (), stored_search,
                   storage_slots=(StorageSlot('bm25', LEXICAL_BM25_V1), StorageSlot('dense', VECTOR_V1),
                                  StorageSlot('source', DOCUMENT_V1))),
        PluginSpec('platform.search.opensearch', ('platform.search',), (), opensearch),
        *(PluginSpec('platform.capabilities.' + key, ('platform.embedding', 'platform.ocr'), names,
                     capabilities(*names)) for key, names in (
                         ('none', ()), ('embedding', ('embedding',)), ('ocr', ('ocr',)),
                         ('models', ('embedding', 'ocr')))),
        PluginSpec('platform.service', ('platform',),
                   ('platform.repository', 'platform.search', 'platform.originals', 'platform.parser',
                    'platform.embedding_jobs', 'platform.embedding', 'platform.ocr'), service),
        PluginSpec('platform.borrowed', ('platform',), (), borrowed),
        PluginSpec('platform.research.borrowed', ('research',), (), borrowed_research),
        PluginSpec('platform.research', ('research',),
                   ('platform', 'platform.embedding', 'models', 'knowledge.model'), research_service),
    ), schemas, checks, {'platform.model_clients.remote': client_inputs})


class HostedPlatform:
    def __init__(self, host):
        self.host = host

    def close(self):
        self.host.close()

    def __getattr__(self, name):
        value = getattr(self.host.service('platform'), name)
        if callable(value) and not name.startswith('_'):
            return lambda *args, **kwargs: self.host.call('platform', name, *args, **kwargs)
        return value
