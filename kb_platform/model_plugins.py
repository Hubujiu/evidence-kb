from .plugin_host import PluginSpec
from .plugin_configuration import configured_plugins, config_schema, TEXT, POSITIVE, CONTEXT_PROPERTIES, check_context, file_inputs, environment_inputs


def model_plugins(models=None, *, clients=None):
    def service(ctx, config):
        selected = models
        borrowed = selected is not None and not config
        if selected is not None and config:


            selected = None
        if 'providers_config' in config:
            from .remote import LunaClient
            from .model_configuration import from_clients

            client = LunaClient(config['providers_config'])
            ctx.defer(client.close)
            selected = from_clients(client)
            ctx.defer(selected.close)
        if 'config_path' in config:
            from .model_configuration import from_config

            selected = from_config(config['config_path'])
            ctx.defer(selected.close)
        if selected is None:
            from .model_service import ModelService

            selected = ModelService(owner_identity=ctx.identity)
            ctx.defer(selected.close)
        if not borrowed:
            selected.adopt(ctx)
        ctx.provide('models', selected)

    def borrowed_clients(ctx, config):
        if clients is None:
            raise ValueError('MODEL_CLIENTS_REQUIRED')
        ctx.provide('model.clients', clients)

    def route(ctx, config):
        from .model_adapters import EmbeddingAdapter, LunaAdapter

        mapping = ctx.require('model.clients')
        if config['client'] not in mapping or mapping[config['client']] is None:
            raise ValueError('MODEL_CLIENT_UNAVAILABLE')
        capability = config.get('capability', 'chat')
        adapters = {'chat': LunaAdapter, 'embedding': EmbeddingAdapter}
        adapter = adapters[capability](mapping[config['client']])
        dispose = ctx.require('models').register(config['route'], adapter,
            registration_id=ctx.registration_id(kind='model_route'),
            owner_identity=ctx.identity, owner_lease=ctx.lease)
        ctx.defer(dispose)
        for marker in ctx.spec.provides:
            ctx.provide(marker, ctx.identity)

    def structured(ctx, config):
        from .context_budget import ContextBudget

        ctx.provide('llm', ctx.require('models').structured(config.get('route', 'generation'),
                    policy=ContextBudget(**config.get('policy', {}))))

    def embedding(ctx, config):
        from .model_configuration import EmbeddingView

        ctx.provide('embedding', EmbeddingView(ctx.require('models'), config.get('route', 'embedding'),
                                               dimension=config['dimension']))

    def ocr(ctx, config):
        from .model_configuration import OCRView

        ctx.provide('ocr', OCRView(ctx.require('models'), config.get('route', 'ocr')))

    def check_service(config):
        if 'config_path' in config:
            from .model_configuration import read_model_config

            read_model_config(config['config_path'])
        if 'providers_config' in config:
            from .remote import read_provider_config

            read_provider_config(config['providers_config'], 'luna')

    def service_inputs(config):
        check_service(config)
        inputs = file_inputs([config[key] for key in ('config_path', 'providers_config') if key in config])
        if 'config_path' in config:
            from .model_configuration import read_model_config

            selected = read_model_config(config['config_path'])
            inputs.update(environment_inputs({value['api_key_env'] for value in selected['providers'].values()}))
        return inputs

    route_properties = {'route': TEXT, 'client': TEXT,
                        'capability': {'enum': ['chat', 'embedding']}}
    schemas = {
        'models.service': config_schema({'config_path': TEXT, 'providers_config': TEXT}, maxProperties=1),
        'models.route.client': config_schema(route_properties, required=('route', 'client')),
        'models.route.generation': config_schema(
            {**route_properties, 'capability': {'const': 'chat'}}, required=('route', 'client')),
        'models.route.embedding': config_schema(
            {**route_properties, 'capability': {'const': 'embedding'}}, required=('route', 'client', 'capability')),
        'models.structured': config_schema({'route': TEXT, 'policy': config_schema(CONTEXT_PROPERTIES)}),
        'models.embedding': config_schema({'route': TEXT, 'dimension': POSITIVE}, required=('dimension',)),
        'models.ocr': config_schema({'route': TEXT}),
    }
    checks = {'models.service': check_service,
              'models.structured': lambda config: check_context(config.get('policy', {}))}
    return configured_plugins((PluginSpec('models.service', ('models',), (), service),
            PluginSpec('models.clients', ('model.clients',), (), borrowed_clients),
            PluginSpec('models.route.client', (), ('models', 'model.clients'), route),
            PluginSpec('models.route.generation', ('models.route.generation',), ('models', 'model.clients'), route),
            PluginSpec('models.route.embedding', ('models.route.embedding',), ('models', 'model.clients'), route),
            PluginSpec('models.structured', ('llm',), ('models',), structured),
            PluginSpec('models.embedding', ('embedding',), ('models',), embedding),
            PluginSpec('models.ocr', ('ocr',), ('models',), ocr)), schemas, checks, {'models.service': service_inputs})
