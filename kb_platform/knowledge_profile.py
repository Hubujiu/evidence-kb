import os
from pathlib import Path

from .plugin_host import PluginHost, PluginSpec
from .knowledge_application import (HostedKnowledgeApplication, KnowledgeApplication, KnowledgeTools,
                                    KnowledgeGraphDriver)
from .plugin_profiles import expand_profile


DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / 'config/agent-profiles/knowledge.json'
ENVIRONMENT = {
    'models.service': {'providers_config': ('KB_AGENT_PROVIDERS_CONFIG', str)},
    'knowledge.context.policy': {
        'input_budget': ('KB_AGENT_INPUT_BUDGET', int),
        'output_reserve': ('KB_AGENT_OUTPUT_RESERVE', int),
        'model_window': ('KB_AGENT_MODEL_WINDOW', int),
        'safety_reserve': ('KB_AGENT_SAFETY_RESERVE', int),
    },
    'knowledge.budget': {'max_calls': ('KB_AGENT_MAX_CALLS', int),
                         'max_tokens': ('KB_AGENT_MAX_TOKENS', int)},
    'harness.store.sqlite': {'root': ('KB_AGENT_ROOT', str)},
    'knowledge.semantic': {'config_path': ('KB_SEMANTIC_PAGE_CONFIG', str)},
}


def load_knowledge_profile(profile=None, *, environment=None):
    environment = os.environ if environment is None else environment
    if profile is None:
        profile = environment.get('KB_AGENT_PROFILE') or DEFAULT_PROFILE
    value = expand_profile(profile, {'knowledge': DEFAULT_PROFILE.parent.parent / 'agent-bundles/knowledge.json'})
    for entry in value['plugins']:
        config = entry.setdefault('config', {})
        for key, (name, convert) in ENVIRONMENT.get(entry.get('use'), {}).items():
            if environment.get(name):
                config[key] = convert(environment[name])
    return value


def _config(config, allowed=()):
    if set(config) - set(allowed):
        raise ValueError('UNKNOWN_KNOWLEDGE_PLUGIN_CONFIG')
    return config


def knowledge_plugins(platform, research, *, service_factory=None):

    def resources(ctx, config):
        _config(config)
        ctx.provide('knowledge.platform', platform)
        ctx.provide('knowledge.research', research)

    def policy(ctx, config):
        from .agent_context import ContextPolicy

        _config(config, ('input_budget', 'output_reserve', 'model_window', 'safety_reserve',
                         'strict_tokens', 'evidence_strategy'))
        ctx.provide('knowledge.context.policy', ContextPolicy(**config))

    def context(ctx, config):
        from .agent_context import ContextManager

        _config(config)
        ctx.provide('knowledge.context', ContextManager(ctx.require('knowledge.context.policy')))

    def budget(ctx, config):
        from .agent_model import BudgetLedger

        _config(config, ('max_calls', 'max_tokens'))
        ctx.provide('knowledge.budget', BudgetLedger(**config))

    def model(ctx, config):
        _config(config, ('route',))
        selected = ctx.require('models').structured(config.get('route', 'generation'),
            policy=ctx.require('knowledge.context.policy'), ledger=ctx.require('knowledge.budget'))
        ctx.provide('knowledge.model', selected)
        ctx.provide('llm', selected)

    def store(ctx, config):
        from .knowledge_sessions import KnowledgeSessions

        _config(config)
        ctx.provide('knowledge.store', KnowledgeSessions(ctx.require('session.store')))

    def workflow(ctx, config):
        from .knowledge_workflow import KnowledgeWorkflow

        _config(config)
        ctx.provide('knowledge.workflow', KnowledgeWorkflow)

    def driver(ctx, config):
        _config(config)
        ctx.provide('loop', KnowledgeGraphDriver(ctx.host))

    def gateway(ctx, config):
        from .agent_tools import EvidenceGateway

        _config(config, ('provider_id',))
        selected_model = ctx.require('knowledge.model')
        ctx.provide('knowledge.gateway', EvidenceGateway(
            ctx.require('knowledge.platform'), ctx.require('knowledge.research'),
            provider_id=config.get('provider_id', getattr(selected_model, 'provider_id', 'magpie_luna'))))

    def executor(ctx, config):
        from .provider_runtime import ToolExecutor

        _config(config, ('max_attempts',))
        ctx.provide('knowledge.executor', ToolExecutor(**config))

    def semantic(ctx, config):
        from .semantic_page_runtime import from_config

        _config(config, ('config_path',))
        service = from_config(ctx.require('knowledge.platform'), ctx.require('knowledge.gateway'),
                              config['config_path']) if config.get('config_path') else None
        if service is not None:
            ctx.defer(service.provider.close)
        ctx.provide('knowledge.semantic', service)

    def knowledge(ctx, config):
        from .knowledge_service import KnowledgeService

        _config(config, ('retrieval_config',))
        factory = KnowledgeService if service_factory is None else service_factory
        session_store = ctx.require('knowledge.store')
        service = factory(
            ctx.require('knowledge.platform'), ctx.require('knowledge.model'),
            research=ctx.require('knowledge.research'), root=session_store.root, store=session_store,
            context_policy=ctx.require('knowledge.context.policy'),
            context_manager=ctx.require('knowledge.context'), runtime=ctx.require('runtime'),
            gateway=ctx.require('knowledge.gateway'), tool_executor=ctx.require('knowledge.executor'),
            semantic_pages=ctx.require('knowledge.semantic'),
            workflow_factory=ctx.require('knowledge.workflow'), **config)
        ctx.provide('knowledge', service)

    def tools(ctx, config):
        _config(config)
        ctx.provide('knowledge.tools', KnowledgeTools(ctx.require('knowledge')))

    def application(ctx, config):
        _config(config)
        ctx.provide('application', KnowledgeApplication(
            ctx.require('knowledge'), ctx.require('knowledge.tools'), ctx.require('loop')))

    return (
        PluginSpec('knowledge.resources', ('knowledge.platform', 'knowledge.research'), (), resources),
        PluginSpec('knowledge.context.policy', ('knowledge.context.policy',), (), policy),
        PluginSpec('knowledge.context', ('knowledge.context',), ('knowledge.context.policy',), context),
        PluginSpec('knowledge.budget', ('knowledge.budget',), (), budget),
        PluginSpec('knowledge.model', ('knowledge.model', 'llm'),
                   ('models', 'knowledge.context.policy', 'knowledge.budget'), model),
        PluginSpec('knowledge.sessions', ('knowledge.store',), ('session.store',), store),
        PluginSpec('knowledge.workflow.graph', ('knowledge.workflow',), (), workflow),
        PluginSpec('knowledge.driver.graph', ('loop',), (), driver),
        PluginSpec('knowledge.gateway', ('knowledge.gateway',),
                   ('knowledge.platform', 'knowledge.research', 'knowledge.model'), gateway),
        PluginSpec('knowledge.executor', ('knowledge.executor',), (), executor),
        PluginSpec('knowledge.semantic', ('knowledge.semantic',),
                   ('knowledge.platform', 'knowledge.gateway'), semantic),
        PluginSpec('knowledge.service', ('knowledge',),
                   ('knowledge.platform', 'knowledge.research', 'knowledge.model', 'knowledge.store',
                    'knowledge.context.policy', 'knowledge.context', 'runtime', 'knowledge.gateway',
                    'knowledge.executor', 'knowledge.semantic', 'knowledge.workflow'), knowledge),
        PluginSpec('knowledge.tools', ('knowledge.tools',), ('knowledge',), tools),
        PluginSpec('knowledge.application', ('application',), ('knowledge', 'knowledge.tools', 'loop'), application),
    )


def create_knowledge_application(platform, research, *, profile=None, service_factory=None,
                                 extra_plugins=(), environment=None, models=None):
    from .harness_services import builtin_plugins

    selected_environment = dict(os.environ if environment is None else environment)
    if models is not None:


        selected_environment.pop('KB_AGENT_PROVIDERS_CONFIG', None)
    selected = load_knowledge_profile(profile, environment=selected_environment)
    if models is not None and any(row['use'] == 'models.service' and row.get('config')
                                  for row in selected['plugins']):
        raise ValueError('MODEL_SERVICE_ALREADY_CONFIGURED')
    from .model_plugins import model_plugins
    if models is None and research is not None:
        from .model_configuration import from_clients
        models = from_clients(research.luna, getattr(research, 'embedding', None))
    catalog = (*builtin_plugins(), *model_plugins(models), *knowledge_plugins(platform, research, service_factory=service_factory),
               *extra_plugins)
    host = PluginHost(selected, catalog)
    try:
        application = host.service('application')
        return HostedKnowledgeApplication(host, application), host
    except BaseException:
        host.close()
        raise
