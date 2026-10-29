"""Allowlisted knowledge plugins and environment-compatible profile composition."""
from copy import deepcopy
import json
import os
from pathlib import Path

from .plugin_host import PluginHost, PluginSpec
from .knowledge_application import HostedKnowledgeApplication, KnowledgeApplication, KnowledgeTools


DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / 'config/agent-profiles/knowledge.json'
ENVIRONMENT = {
    'knowledge.provider.luna': {'providers_config': ('KB_AGENT_PROVIDERS_CONFIG', str)},
    'knowledge.context.policy': {
        'input_budget': ('KB_AGENT_INPUT_BUDGET', int),
        'output_reserve': ('KB_AGENT_OUTPUT_RESERVE', int),
        'model_window': ('KB_AGENT_MODEL_WINDOW', int),
        'safety_reserve': ('KB_AGENT_SAFETY_RESERVE', int),
    },
    'knowledge.budget': {'max_calls': ('KB_AGENT_MAX_CALLS', int),
                         'max_tokens': ('KB_AGENT_MAX_TOKENS', int)},
    'knowledge.store': {'root': ('KB_AGENT_ROOT', str)},
    'knowledge.semantic': {'config_path': ('KB_SEMANTIC_PAGE_CONFIG', str)},
}


def load_knowledge_profile(profile=None, *, environment=None):
    environment = os.environ if environment is None else environment
    if profile is None:
        profile = environment.get('KB_AGENT_PROFILE') or DEFAULT_PROFILE
    value = deepcopy(profile) if isinstance(profile, dict) else json.loads(Path(profile).read_text('utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('plugins'), list):
        raise ValueError('INVALID_KNOWLEDGE_PROFILE')
    for entry in value['plugins']:
        if not isinstance(entry, dict):
            raise ValueError('INVALID_KNOWLEDGE_PROFILE')
        config = entry.setdefault('config', {})
        if not isinstance(config, dict):
            raise ValueError('INVALID_KNOWLEDGE_PLUGIN_CONFIG')
        for key, (name, convert) in ENVIRONMENT.get(entry.get('use'), {}).items():
            if environment.get(name):
                config[key] = convert(environment[name])
    return value


def _config(config, allowed=()):
    if set(config) - set(allowed):
        raise ValueError('UNKNOWN_KNOWLEDGE_PLUGIN_CONFIG')
    return config


def knowledge_plugins(platform, research, *, service_factory=None):
    """External resources are borrowed; a plugin closes only clients it creates."""
    def resources(ctx, config):
        _config(config)
        ctx.provide('knowledge.platform', platform)
        ctx.provide('knowledge.research', research)

    def provider(ctx, config):
        from .agent_luna import LunaModelProvider

        _config(config, ('providers_config',))
        selected = config.get('providers_config')
        if selected:
            from .remote import LunaClient

            reader = LunaClient(selected)
            ctx.defer(reader.close)
        else:
            reader = ctx.require('knowledge.research').luna
        ctx.provide('knowledge.provider', LunaModelProvider(reader))

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
        from .agent_model import AgentModel

        _config(config)
        ctx.provide('knowledge.model', AgentModel(
            ctx.require('knowledge.provider'), policy=ctx.require('knowledge.context.policy'),
            ledger=ctx.require('knowledge.budget')))

    def store(ctx, config):
        from .agent_store import AgentStore

        _config(config, ('root',))
        ctx.provide('knowledge.store', AgentStore(**config))

    def gateway(ctx, config):
        from .agent_tools import EvidenceGateway

        _config(config, ('provider_id',))
        ctx.provide('knowledge.gateway', EvidenceGateway(
            ctx.require('knowledge.platform'), ctx.require('knowledge.research'),
            provider_id=config.get('provider_id', 'magpie_luna')))

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
        ctx.provide('knowledge', factory(
            ctx.require('knowledge.platform'), ctx.require('knowledge.model'),
            research=ctx.require('knowledge.research'), root=session_store.root, store=session_store,
            context_policy=ctx.require('knowledge.context.policy'),
            context_manager=ctx.require('knowledge.context'), runtime=ctx.require('runtime'),
            gateway=ctx.require('knowledge.gateway'), tool_executor=ctx.require('knowledge.executor'),
            semantic_pages=ctx.require('knowledge.semantic'), **config))

    def tools(ctx, config):
        _config(config)
        ctx.provide('knowledge.tools', KnowledgeTools(ctx.require('knowledge')))

    def application(ctx, config):
        _config(config)
        ctx.provide('application', KnowledgeApplication(
            ctx.require('knowledge'), ctx.require('knowledge.tools'), ctx.require('loop')))

    return (
        PluginSpec('knowledge.resources', ('knowledge.platform', 'knowledge.research'), (), resources),
        PluginSpec('knowledge.provider.luna', ('knowledge.provider',), ('knowledge.research',), provider),
        PluginSpec('knowledge.context.policy', ('knowledge.context.policy',), (), policy),
        PluginSpec('knowledge.context', ('knowledge.context',), ('knowledge.context.policy',), context),
        PluginSpec('knowledge.budget', ('knowledge.budget',), (), budget),
        PluginSpec('knowledge.model', ('knowledge.model',),
                   ('knowledge.provider', 'knowledge.context.policy', 'knowledge.budget'), model),
        PluginSpec('knowledge.store', ('knowledge.store',), (), store),
        PluginSpec('knowledge.gateway', ('knowledge.gateway',),
                   ('knowledge.platform', 'knowledge.research'), gateway),
        PluginSpec('knowledge.executor', ('knowledge.executor',), (), executor),
        PluginSpec('knowledge.semantic', ('knowledge.semantic',),
                   ('knowledge.platform', 'knowledge.gateway'), semantic),
        PluginSpec('knowledge.service', ('knowledge',),
                   ('knowledge.platform', 'knowledge.research', 'knowledge.model', 'knowledge.store',
                    'knowledge.context.policy', 'knowledge.context', 'runtime', 'knowledge.gateway',
                    'knowledge.executor', 'knowledge.semantic'), knowledge),
        PluginSpec('knowledge.tools', ('knowledge.tools',), ('knowledge',), tools),
        PluginSpec('knowledge.application', ('application',), ('knowledge', 'knowledge.tools', 'loop'), application),
    )


def create_knowledge_application(platform, research, *, profile=None, service_factory=None,
                                 extra_plugins=(), environment=None):
    from .harness_services import builtin_plugins

    selected = load_knowledge_profile(profile, environment=environment)
    catalog = (*builtin_plugins(), *knowledge_plugins(platform, research, service_factory=service_factory),
               *extra_plugins)
    host = PluginHost(selected, catalog)
    try:
        application = host.service('application')
        return HostedKnowledgeApplication(host, application), host
    except BaseException:
        host.close()
        raise
