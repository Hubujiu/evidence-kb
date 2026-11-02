import json
from pathlib import Path

from .application_storage import ApplicationStorage, StorageClient, storage_methods
from .llm_contract import LlmEndpoint, METHODS as LLM_METHODS, POLICY_METHODS
from .plugin_configuration import TEXT, config_schema
from .plugin_contracts import ContractApplication, ContractRegistry


CATALOG = [
    {'implementation': 'foundation.storage', 'version': '1', 'executor': 'local',
     'provides': {'storage': {'id': 'storage.service', 'major': 1}}, 'requires': {}},
    {'implementation': 'foundation.llm', 'version': '1', 'executor': 'local',
     'provides': {'llm': {'id': 'llm.service', 'major': 1}},
     'requires': {'storage': {'contract': {'id': 'storage.service', 'major': 1}, 'optional': True},
                  'policy': {'contract': {'id': 'llm.policy', 'major': 1}, 'optional': True}}},
]
SCHEMAS = {
    ('foundation.storage', '1'): config_schema({'path': TEXT}, required=('path',)),
    ('foundation.llm', '1'): config_schema({'config_path': TEXT, 'providers_config': TEXT,
        'audit_root': TEXT, 'budget_binding': TEXT}),
}


def registry_extensions():
    return {('storage.service', 1): storage_methods(), ('llm.service', 1): LLM_METHODS,
            ('llm.policy', 1): POLICY_METHODS}


def foundation_mounts(*, storage_configuration=None, provider_catalog=None, models=None, model_clients=None,
                       storage_requirements=None, storage_schemas=None):
    def storage(ctx, config):
        configuration = storage_configuration
        if configuration is None:
            configuration = json.loads(Path(config['path']).read_text('utf-8'))
        service = ApplicationStorage(configuration, provider_catalog=provider_catalog,
                                     requirements=storage_requirements, schemas=storage_schemas)
        ctx.defer(service.close)
        ctx.provide('storage', service)

    def llm(ctx, config):
        from .model_configuration import configured_adapter, configured_budget, read_model_config
        from .model_service import ModelService

        if models is not None and set(config) & {'config_path', 'providers_config'}:
            raise ValueError('MODEL_SERVICE_ALREADY_CONFIGURED')
        registry = models
        metadata, statistics = {}, {}
        if registry is None:
            registry = ModelService()
            ctx.defer(registry.close)
            if config.get('config_path'):
                if config.get('providers_config') or 'budget_binding' not in config:
                    raise ValueError('MODEL_BUDGET_BINDING_REQUIRED')
                storage = ctx.require('storage')
                if storage is None:
                    raise ValueError('MODEL_BUDGET_STORAGE_REQUIRED')
                value = read_model_config(config['config_path'])
                database = StorageClient(storage, config['budget_binding']).relational()
                ledger = configured_budget(value, database=database)
                ctx.defer(ledger.close)
                for name, row in value['models'].items():
                    adapter = configured_adapter(value, name, ledger, audit_root=config.get('audit_root'))
                    ctx.defer(adapter.close)
                    ctx.defer(registry.register(name, adapter))
                    metadata[name] = {'dimension': row.get('dimension')}
            else:
                from .model_adapters import EmbeddingAdapter, LunaAdapter
                from .model_clients import OwnedModelClients
                from .remote import GoogleEmbeddingProvider, LunaClient

                if model_clients is None:
                    if not config.get('providers_config'):
                        raise ValueError('MODEL_CONFIGURATION_REQUIRED')
                    generation = LunaClient(config['providers_config'])
                    ctx.defer(generation.close)
                    embedding = GoogleEmbeddingProvider(config['providers_config'])
                    ctx.defer(embedding.close)
                elif isinstance(model_clients, OwnedModelClients):
                    generation, embedding = model_clients.claim(ctx)
                else:
                    generation, embedding = model_clients
                ctx.defer(registry.register('generation', LunaAdapter(generation)))
                metadata['generation'] = {'manifest': generation.manifest, 'maximum_format_corrections': 1}
                if callable(getattr(generation, 'snapshot_stats', None)):
                    statistics['generation'] = generation.snapshot_stats
                if embedding is not None:
                    ctx.defer(registry.register('embedding', EmbeddingAdapter(embedding)))
                    metadata['embedding'] = {'dimension': embedding.dimension,
                                              'embedding_manifest': embedding.manifest}
        endpoint = LlmEndpoint(registry, metadata=metadata, statistics=statistics, policy=ctx.require('policy'))
        ctx.defer(endpoint.close)
        ctx.provide('llm', endpoint)

    return {('foundation.storage', '1'): storage, ('foundation.llm', '1'): llm}


def build_foundation_application(application, *, extra_catalog=(), extra_mounts=None, schemas=None,
                                  contracts=None, process_root=None, **options):
    return ContractApplication(application, [*CATALOG, *extra_catalog],
        registry=ContractRegistry({**registry_extensions(), **(contracts or {})}),
        mounts={**foundation_mounts(**options), **(extra_mounts or {})},
        schemas={**SCHEMAS, **(schemas or {})}, process_root=process_root)
