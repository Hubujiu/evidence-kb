from copy import deepcopy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path

from .harness_services import builtin_plugins
from .knowledge_application import HostedKnowledgeApplication
from .knowledge_profile import ENVIRONMENT, knowledge_plugins, load_knowledge_profile
from .model_plugins import model_plugins
from .storage_cache_plugins import stage_cache_plugins
from .platform_plugins import HostedPlatform, platform_plugins
from .plugin_host import PluginHost
from .plugin_profiles import expand_profile
from .plugin_process import ProcessPackages


CONFIG = Path(__file__).resolve().parents[1] / 'config'


@dataclass(frozen=True)
class ApplicationComposition:
    host: PluginHost
    platform: object
    research: object
    application: object
    models: object

    def close(self):
        self.host.close()


def _row(identity, use, **config):
    return {'id': identity, 'use': use, 'config': config}


def _overlay(selected, environment):
    for row in selected['plugins']:
        config = row.setdefault('config', {})
        for key, (name, convert) in ENVIRONMENT.get(row['use'], {}).items():
            if environment.get(name):
                config[key] = convert(environment[name])
    return selected


def _client_routes():
    return [_row('generation-route', 'models.route.generation', route='generation',
                 client='generation', capability='chat'),
            _row('embedding-route', 'models.route.embedding', route='embedding',
                 client='embedding', capability='embedding')]


def _search_channels(selected, settings, environment):
    path = environment.get('KB_SEARCH_CHANNELS_CONFIG') or getattr(settings, 'search_channels_file', None)
    if not path:
        return selected
    channels = json.loads(Path(path).read_text('utf-8'))
    if not isinstance(channels, dict) or set(channels) != {'bm25', 'dense'}:
        raise ValueError('INVALID_SEARCH_CHANNELS_CONFIGURATION')
    search = next((row for row in selected['plugins'] if row['id'] == 'search'), None)
    if search is None:
        raise ValueError('SEARCH_SERVICE_REQUIRED')
    search.update(use='platform.search.split', config={})
    for channel in ('bm25', 'dense'):
        value = dict(channels[channel])
        backend = value.pop('backend', None)
        if backend not in ('postgres', 'embedded'):
            raise ValueError('INVALID_SEARCH_CHANNEL_BACKEND')
        selected['plugins'].append(_row('search_' + channel,
            'platform.search.' + channel + '.' + backend, **value))
    selected['plugins'].append(_row('search_source', 'platform.search.source.postgres'))
    return selected


def _storage(selected, catalog, settings, environment):
    from .storage_catalog import default_storage_providers
    from .storage_plugins import compile_storage_plugins
    from .storage_sdk import RELATIONAL_POSTGRES_V1, RELATIONAL_V1, StorageSlot

    if environment.get('KB_DATABASES_CONFIG'):
        raise ValueError('LEGACY_STORAGE_CONFIGURATION_REQUIRES_CONVERSION')
    path = environment.get('KB_STORAGE_CONFIG')
    if not path:
        return selected, catalog
    configuration = json.loads(Path(path).read_text('utf-8'))
    by_id = {row['id']: row for row in selected['plugins']}
    changes = {}
    for identity, bindings in configuration['bindings'].items():
        if identity not in by_id:
            raise ValueError('UNKNOWN_STORAGE_CONSUMER')
        row = by_id[identity]
        use = row['use']
        if use in ('harness.store.sqlite', 'harness.store.database'):
            row.update(use='harness.store.database', config={})
            changes[row['use']] = (StorageSlot('primary', RELATIONAL_V1),)
        elif use == 'models.service':
            row.setdefault('config', {})['database_slot'] = 'budget'
            changes[use] = (StorageSlot('budget', RELATIONAL_V1),)
        elif use in ('platform.repository.postgres', 'platform.search.postgres',
                     'platform.search.bm25.postgres', 'platform.search.dense.postgres',
                     'platform.search.source.postgres'):
            row.setdefault('config', {})['database_slot'] = 'primary'
            changes[use] = (StorageSlot('primary', RELATIONAL_POSTGRES_V1),)
        elif use in ('platform.originals.files', 'platform.originals.blobs'):
            row.update(use='platform.originals.storage', config={})
        elif use == 'platform.search.split':
            row.update(use='platform.search.storage', config={})
    catalog = tuple(replace(spec, storage_slots=changes[spec.key]) if spec.key in changes else spec
                    for spec in catalog)
    return compile_storage_plugins(selected, catalog, configuration,
                                   provider_catalog=default_storage_providers())


def load_application_profile(settings, *, profile=None, environment=None, remote=None):
    environment = dict(os.environ if environment is None else environment)
    remote = environment.get('KB_REMOTE_MODELS') == '1' if remote is None else remote
    model_path = environment.get('KB_MODELS_CONFIG') if remote else None
    configured = json.loads(Path(model_path).read_text('utf-8'))['models'] if model_path else None
    generation = remote and (configured is None or 'generation' in configured)
    path = profile or environment.get('KB_APPLICATION_PROFILE') or CONFIG / 'application-profiles' / (
        'knowledge.json' if generation else 'bm25.json')
    selected = expand_profile(path, {'platform': CONFIG / 'application-bundles/platform.json',
                                    'knowledge': CONFIG / 'agent-bundles/knowledge.json'})
    if profile is None and not environment.get('KB_APPLICATION_PROFILE') and generation and environment.get('KB_AGENT_PROFILE'):
        selected = expand_profile(CONFIG / 'application-profiles/bm25.json',
                                  {'platform': CONFIG / 'application-bundles/platform.json'})
        next(row for row in selected['plugins'] if row['id'] == 'research')['use'] = 'platform.research'
        selected['plugins'].extend(load_knowledge_profile(environment=environment)['plugins'])
    selected = _overlay(selected, environment)
    by_id = {row['id']: row for row in selected['plugins']}
    if 'search' in by_id and profile is None and not environment.get('KB_APPLICATION_PROFILE'):
        if settings.search_backend not in ('postgres', 'embedded', 'opensearch'):
            raise ValueError('UNKNOWN_SEARCH_BACKEND')
        by_id['search']['use'] = 'platform.search.' + settings.search_backend
    selected = _search_channels(selected, settings, environment)
    if not remote or 'platform' not in by_id:
        return selected
    if 'provider' not in by_id:
        selected['plugins'].append(_row('provider', 'models.service'))
        by_id['provider'] = selected['plugins'][-1]
    if model_path:
        by_id['provider']['config'] = {'config_path': model_path}
        embedding, ocr = 'embedding' in configured, 'ocr' in configured
        if embedding:
            selected['plugins'].append(_row('embedding', 'models.embedding',
                                           dimension=configured['embedding']['dimension']))
        if ocr:
            selected['plugins'].append(_row('ocr', 'models.ocr'))
    else:
        by_id['provider']['config'] = {}
        source = environment.get('KB_PROVIDERS_CONFIG', '.local/providers.json')
        selected['plugins'].extend([
            _row('model-clients', 'platform.model_clients.remote', providers_config=source,
                 generation_config=environment.get('KB_AGENT_PROVIDERS_CONFIG') or source),
            *_client_routes(), _row('embedding', 'platform.embedding.client'),
            _row('ocr', 'platform.ocr.client')])
        if 'model' in by_id and by_id['model']['use'] == 'knowledge.model':
            by_id['model']['use'] = 'knowledge.model.route'
        embedding, ocr = True, True
    capability = 'models' if embedding and ocr else 'embedding' if embedding else 'ocr' if ocr else 'none'
    by_id['capabilities']['use'] = 'platform.capabilities.' + capability
    if generation:
        by_id['research']['config'] = {'root': environment.get('KB_RESEARCH_ROOT', '.local/research-v4e-pruned')}
    return selected


def create_root_application(settings, *, profile=None, environment=None, platform=None,
                            research=None, models=None, model_clients=None, service_factory=None,
                            extra_plugins=(), remote=None):
    selected = load_application_profile(settings, profile=profile, environment=environment, remote=remote)
    if platform is not None:
        excluded = {'repository', 'originals', 'parser', 'embedding-jobs', 'search', 'search_bm25', 'search_dense', 'capabilities'}
        selected['plugins'] = [row for row in selected['plugins'] if row['id'] not in excluded]
        next(row for row in selected['plugins'] if row['id'] == 'platform').update(use='platform.borrowed')
    selected_environment = os.environ if environment is None else environment
    packages_root = selected_environment.get('KB_PROCESS_PLUGINS_ROOT')
    installed = ProcessPackages(packages_root).catalogs(
        journal=Path(packages_root) / 'transport-journal') if packages_root else ()
    catalog = (*builtin_plugins(), *model_plugins(models), *stage_cache_plugins(),
               *platform_plugins(settings, platform=platform, research=research, model_clients=model_clients),
               *knowledge_plugins(platform, research, service_factory=service_factory), *installed, *extra_plugins)
    selected, catalog = _storage(selected, catalog, settings, selected_environment)
    host = PluginHost(selected, catalog)
    try:
        specs = {spec.key: spec for spec in catalog}
        provided = {name for row in selected['plugins'] if not row.get('disabled')
                    for name in specs[row['use']].provides}
        application = HostedKnowledgeApplication(host, host.service('application')) if 'application' in provided else None
        return ApplicationComposition(
            host, HostedPlatform(host) if 'platform' in provided else None,
            host.service('research') if 'research' in provided else None, application,
            host.service('models') if 'models' in provided else None)
    except BaseException:
        host.close()
        raise


def create_borrowed_knowledge(platform, research, *, profile=None, service_factory=None,
                              extra_plugins=(), environment=None, models=None):
    from .settings import Settings

    selected_environment = dict(os.environ if environment is None else environment)
    selected_environment.pop('KB_AGENT_PROVIDERS_CONFIG', None)
    selected = load_knowledge_profile(profile, environment=selected_environment)
    clients = {'generation': research.luna} if research is not None else {}
    if research is not None and getattr(research, 'embedding', None) is not None:
        clients['embedding'] = research.embedding
    provider = next((row for row in selected['plugins'] if row['use'] == 'models.service'), None)
    if provider is not None and models is not None and provider.get('config'):
        raise ValueError('MODEL_SERVICE_ALREADY_CONFIGURED')
    if provider is not None and not provider.get('config') and models is None:
        for row in selected['plugins']:
            if row['use'] == 'knowledge.model':
                row['use'] = 'knowledge.model.route'
        selected['plugins'].append(_row('model-clients', 'models.clients'))
        selected['plugins'].extend(row for row in _client_routes() if row['config']['client'] in clients)
    selected['plugins'].extend([_row('platform', 'platform.borrowed'),
                                _row('research', 'platform.research.borrowed')])
    catalog = (*builtin_plugins(), *model_plugins(models, clients=clients), *stage_cache_plugins(),
               *platform_plugins(Settings.from_environment(), platform=platform, research=research),
               *knowledge_plugins(platform, research, service_factory=service_factory), *extra_plugins)
    if selected_environment.get('KB_STORAGE_CONFIG'):
        selected, catalog = _storage(selected, catalog, None, selected_environment)
    host = PluginHost(deepcopy(selected), catalog)
    try:
        return HostedKnowledgeApplication(host, host.service('application')), host
    except BaseException:
        host.close()
        raise


def create_platform(settings, *, embedding_provider=None):
    from .plugin_host import PluginSpec

    selected = load_application_profile(settings, environment={}, remote=False)
    plugins = ()
    if embedding_provider is not None:
        next(row for row in selected['plugins'] if row['id'] == 'capabilities')['use'] = 'platform.capabilities.injected'
        plugins = (PluginSpec('platform.capabilities.injected', ('platform.embedding', 'platform.ocr'), (),
                   lambda ctx, config: (ctx.provide('platform.embedding', embedding_provider),
                                        ctx.provide('platform.ocr', None))),)
    return create_root_application(settings, profile=selected, environment={}, extra_plugins=plugins, remote=False).platform
