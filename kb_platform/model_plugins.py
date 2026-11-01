from .plugin_host import PluginSpec
from .plugin_configuration import configured_plugins, config_schema, TEXT, POSITIVE, CONTEXT_PROPERTIES, check_context


def model_plugins(models=None, *, clients=None):
    from .model_provider_plugins import model_resource_plugins

    def service(ctx, config):
        selected = None if config.get('owned', False) else models
        borrowed = selected is not None
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

    route_properties = {'route': TEXT, 'client': TEXT,
                        'capability': {'enum': ['chat', 'embedding']}}
    schemas = {
        'models.service': config_schema({'owned': {'type': 'boolean'}}),
        'models.route.client': config_schema(route_properties, required=('route', 'client')),
        'models.route.generation': config_schema(
            {**route_properties, 'capability': {'const': 'chat'}}, required=('route', 'client')),
        'models.route.embedding': config_schema(
            {**route_properties, 'capability': {'const': 'embedding'}}, required=('route', 'client', 'capability')),
        'models.structured': config_schema({'route': TEXT, 'policy': config_schema(CONTEXT_PROPERTIES)}),
        'models.embedding': config_schema({'route': TEXT, 'dimension': POSITIVE}, required=('dimension',)),
        'models.ocr': config_schema({'route': TEXT}),
    }
    checks = {'models.structured': lambda config: check_context(config.get('policy', {}))}
    return (*configured_plugins((PluginSpec('models.service', ('models',), (), service),
            PluginSpec('models.clients', ('model.clients',), (), borrowed_clients),
            PluginSpec('models.route.client', (), ('models', 'model.clients'), route),
            PluginSpec('models.route.generation', ('models.route.generation',), ('models', 'model.clients'), route),
            PluginSpec('models.route.embedding', ('models.route.embedding',), ('models', 'model.clients'), route),
            PluginSpec('models.structured', ('llm',), ('models',), structured),
            PluginSpec('models.embedding', ('embedding',), ('models',), embedding),
            PluginSpec('models.ocr', ('ocr',), ('models',), ocr)), schemas, checks), *model_resource_plugins())
