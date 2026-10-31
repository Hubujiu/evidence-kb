from pathlib import Path

from .plugin_host import PluginSpec
from .plugin_configuration import configured_plugins, config_schema, TEXT, POSITIVE, file_inputs


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
        from .platform_ports import PostgresRepository

        ctx.provide('platform.repository', PostgresRepository(config.get('dsn', settings.dsn)))

    def originals(ctx, config):
        from .storage import OriginalStore

        ctx.provide('platform.originals', OriginalStore(config.get('root', settings.storage_root)))

    def parser(ctx, config):
        from .document_parser import parse

        ctx.provide('platform.parser', parse)

    def jobs(ctx, config):
        from .embedding_jobs import EmbeddingJobs

        ctx.provide('platform.embedding_jobs', EmbeddingJobs(ctx.require('platform.repository').dsn, **config))

    def postgres(ctx, config):
        from .postgres_search import PostgresSearch

        service = PostgresSearch(ctx.require('platform.repository').dsn)
        ctx.defer(service.close)
        ctx.provide('platform.search', service)

    def embedded(ctx, config):
        from .embedded_search import EmbeddedSearch

        service = EmbeddedSearch(config.get('root', Path(settings.storage_root) / '.search-indexes'))
        ctx.defer(service.close)
        ctx.provide('platform.search', service)

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
        'platform.repository.postgres': config_schema({'dsn': {'type': 'string'}}),
        'platform.originals.files': config_schema({'root': TEXT}),
        'platform.embedding_jobs': config_schema({key: POSITIVE for key in
            ('lease_seconds', 'max_attempts', 'circuit_threshold', 'circuit_seconds')}),
        'platform.search.embedded': config_schema({'root': TEXT}),
        'platform.search.opensearch': config_schema({'url': {'type': 'string', 'pattern': '^https?://'}}),
        'platform.research': config_schema({'root': TEXT, 'route': TEXT}),
    }
    checks = {'platform.model_clients.remote': lambda config: client_inputs(config)}
    return configured_plugins((
        PluginSpec('platform.model_clients.remote', ('model.clients',), (), clients),
        PluginSpec('platform.embedding.client', ('embedding',), ('models', 'model.clients', 'models.route.embedding'), embedding_client),
        PluginSpec('platform.ocr.client', ('ocr',), ('models', 'model.clients', 'models.route.generation'), ocr_client),
        PluginSpec('platform.repository.postgres', ('platform.repository',), (), repository),
        PluginSpec('platform.originals.files', ('platform.originals',), (), originals),
        PluginSpec('platform.parser.native', ('platform.parser',), (), parser),
        PluginSpec('platform.embedding_jobs', ('platform.embedding_jobs',), ('platform.repository',), jobs),
        PluginSpec('platform.search.postgres', ('platform.search',), ('platform.repository',), postgres),
        PluginSpec('platform.search.embedded', ('platform.search',), (), embedded),
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
