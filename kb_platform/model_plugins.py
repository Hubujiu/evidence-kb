from .plugin_host import PluginSpec


def model_plugins(models=None):
    def service(ctx, config):
        if set(config) - {'config_path', 'providers_config'} or len(config) > 1:
            raise ValueError('UNKNOWN_MODELS_CONFIG')
        selected = models
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
            raise ValueError('MODEL_SERVICE_REQUIRED')
        ctx.provide('models', selected)

    def structured(ctx, config):
        if set(config) - {'route', 'policy'}:
            raise ValueError('UNKNOWN_LLM_CONFIG')
        from .context_budget import ContextBudget

        ctx.provide('llm', ctx.require('models').structured(config.get('route', 'generation'),
                    policy=ContextBudget(**config.get('policy', {}))))

    def embedding(ctx, config):
        if set(config) - {'route', 'dimension'} or 'dimension' not in config:
            raise ValueError('INVALID_EMBEDDING_CONFIG')
        from .model_configuration import EmbeddingView

        ctx.provide('embedding', EmbeddingView(ctx.require('models'), config.get('route', 'embedding'),
                                               dimension=config['dimension']))

    def ocr(ctx, config):
        if set(config) - {'route'}:
            raise ValueError('INVALID_OCR_CONFIG')
        from .model_configuration import OCRView

        ctx.provide('ocr', OCRView(ctx.require('models'), config.get('route', 'ocr')))

    return (PluginSpec('models.service', ('models',), (), service),
            PluginSpec('models.structured', ('llm',), ('models',), structured),
            PluginSpec('models.embedding', ('embedding',), ('models',), embedding),
            PluginSpec('models.ocr', ('ocr',), ('models',), ocr))
