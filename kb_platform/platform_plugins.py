from .plugin_host import PluginSpec
from .plugin_configuration import configured_plugins, config_schema, TEXT, POSITIVE, file_inputs


from .model_clients import OwnedModelClients as OwnedModelClients


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
        from .storage_plugins import require_storage

        ctx.provide('platform.repository', KnowledgeRepository(database=require_storage(ctx, 'primary')))

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

    def stored_search(ctx, config):
        from .search_router import SplitSearch
        from .search_evidence import DocumentEvidenceSource
        from .storage_plugins import require_storage

        ctx.provide('platform.search', SplitSearch(require_storage(ctx, 'bm25'),
            require_storage(ctx, 'dense'), DocumentEvidenceSource(require_storage(ctx, 'source'))))

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
        'platform.repository.storage': config_schema(),
        'platform.embedding_jobs': config_schema({key: POSITIVE for key in
            ('lease_seconds', 'max_attempts', 'circuit_threshold', 'circuit_seconds')}),
        'platform.research': config_schema({'root': TEXT, 'route': TEXT}),
    }
    from .storage_sdk import DOCUMENT_V1, LEXICAL_BM25_V1, OBJECT_V1, StorageSlot, VECTOR_V1

    from .storage_sdk.relational import STRUCTURED_RELATIONAL_V1

    checks = {'platform.model_clients.remote': client_inputs}
    return configured_plugins((
        PluginSpec('platform.model_clients.remote', ('model.clients',), (), clients),
        PluginSpec('platform.embedding.client', ('embedding',), ('models', 'model.clients', 'models.route.embedding'), embedding_client),
        PluginSpec('platform.ocr.client', ('ocr',), ('models', 'model.clients', 'models.route.generation'), ocr_client),
        PluginSpec('platform.repository.storage', ('platform.repository',), (StorageSlot('primary', STRUCTURED_RELATIONAL_V1),), repository),
        PluginSpec('platform.originals.storage', ('platform.originals',), (StorageSlot('objects', OBJECT_V1),), bound_originals),
        PluginSpec('platform.parser.native', ('platform.parser',), (), parser),
        PluginSpec('platform.embedding_jobs', ('platform.embedding_jobs',), ('platform.repository',), jobs),
        PluginSpec('platform.search.storage', ('platform.search',), (StorageSlot('bm25', LEXICAL_BM25_V1), StorageSlot('dense', VECTOR_V1),
                                  StorageSlot('source', DOCUMENT_V1)), stored_search),
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
