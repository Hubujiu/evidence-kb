import os
from pathlib import Path

from .plugin_host import PluginSpec
from .plugin_configuration import configured_plugins, config_schema, TEXT, POSITIVE, file_inputs
from .knowledge_application import (KnowledgeApplication, KnowledgeTools,
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




def knowledge_plugins(platform, research, *, service_factory=None, tool_plugins=()):

    def resources(ctx, config):
        ctx.provide('knowledge.platform', ctx.require('platform'))
        ctx.provide('knowledge.research', ctx.require('research'))

    def policy(ctx, config):
        from .agent_context import ContextPolicy

        ctx.provide('knowledge.context.policy', ContextPolicy(**config))

    def context(ctx, config):
        from .agent_context import ContextManager

        ctx.provide('knowledge.context', ContextManager(ctx.require('knowledge.context.policy')))

    def budget(ctx, config):
        from .agent_model import BudgetLedger

        ctx.provide('knowledge.budget', BudgetLedger(**config))

    def model(ctx, config):
        selected = ctx.require('models').structured(config.get('route', 'generation'),
            policy=ctx.require('knowledge.context.policy'), ledger=ctx.require('knowledge.budget'))
        ctx.provide('knowledge.model', selected)
        ctx.provide('llm', selected)

    def store(ctx, config):
        from .knowledge_sessions import KnowledgeSessions

        ctx.provide('knowledge.store', KnowledgeSessions(ctx.require('session.store')))

    def workflow(ctx, config):
        from .knowledge_workflow import KnowledgeWorkflow

        ctx.provide('knowledge.workflow', KnowledgeWorkflow)

    def driver(ctx, config):
        ctx.provide('loop', KnowledgeGraphDriver(ctx.host))

    def gateway(ctx, config):
        from .agent_tools import EvidenceGateway

        selected = ctx.require('knowledge.model')
        ctx.provide('knowledge.gateway', EvidenceGateway(
            ctx.require('knowledge.platform'), ctx.require('knowledge.research'),
            provider_id=config.get('provider_id', selected.provider_id),
            embedding_provider=getattr(ctx.require('knowledge.platform'), 'embedding_provider', None),
            pipeline_reranker=ctx.require('knowledge.reranker')))

    def reranker(ctx, config):
        from .model_configuration import TextView
        from .retrieval_pipeline import LunaCandidateReranker

        route = config.get('route', 'generation')
        model = ctx.require('knowledge.model')
        structured = ctx.require('models').structured(route, policy=model.policy, ledger=model.ledger)
        text = TextView(ctx.require('models'), structured, route)
        ctx.provide('knowledge.reranker', LunaCandidateReranker(text))

    def no_reranker(ctx, config):
        ctx.provide('knowledge.reranker', None)

    def executor(ctx, config):
        from .provider_runtime import ToolExecutor

        ctx.provide('knowledge.executor', ToolExecutor(**config))

    def semantic(ctx, config):
        from .semantic_page_runtime import from_config

        service = from_config(ctx.require('knowledge.platform'), ctx.require('knowledge.gateway'),
                              config['config_path']) if config.get('config_path') else None
        if service is not None:
            ctx.defer(service.provider.close)
        ctx.provide('knowledge.semantic', service)

    def knowledge(ctx, config):
        from .knowledge_service import KnowledgeService

        factory = KnowledgeService if service_factory is None else service_factory
        session_store = ctx.require('knowledge.store')
        service = factory(
            ctx.require('knowledge.platform'), ctx.require('knowledge.model'),
            store=session_store,
            context_policy=ctx.require('knowledge.context.policy'),
            context_manager=ctx.require('knowledge.context'), runtime=ctx.require('runtime'),
            gateway=ctx.require('knowledge.gateway'), tool_executor=ctx.require('knowledge.executor'),
            semantic_pages=ctx.require('knowledge.semantic'),
            workflow_factory=ctx.require('knowledge.workflow'),
            tool_plugins=ctx.require('knowledge.tool_plugins'), **config)
        ctx.provide('knowledge', service)

    def tools(ctx, config):
        ctx.provide('knowledge.tools', KnowledgeTools(ctx.require('knowledge')))

    def tool_catalog(ctx, config):
        ctx.provide('knowledge.tool_plugins', tuple(tool_plugins))

    def application(ctx, config):
        ctx.provide('application', KnowledgeApplication(
            ctx.require('knowledge'), ctx.require('knowledge.tools'), ctx.require('loop')))

    def semantic_inputs(config):
        if not config.get('config_path'):
            return {}
        from .semantic_page_runtime import _path, read_semantic_config
        from .remote import read_provider_config

        path, _, selected = read_semantic_config(config['config_path'])
        provider_path = _path(selected['provider_config'])
        _, provider = read_provider_config(provider_path, 'google', credentials=True)
        paths = [path, provider_path]
        if provider.get('platform') == 'vertex_ai':
            paths.append(Path(provider['service_account_file']))
        return file_inputs(paths)

    from .agent_context import ContextPolicy
    from .retrieval_pipeline import PipelineConfig
    from .plugin_configuration import CONTEXT_PROPERTIES

    policy_properties = {**CONTEXT_PROPERTIES,
        'evidence_strategy': {'enum': ['prefix', 'query_blocks', 'coverage_blocks']}}
    retrieval_properties = {
        'channels': {'type': 'array', 'items': {'enum': ['bm25', 'dense']},
                     'minItems': 1, 'uniqueItems': True},
        'candidate_limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'fused_limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'rerank': {'type': 'boolean'}, 'parent': {'enum': ['none', 'paragraph', 'section', 'page']},
        'top_k': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'max_expanded_units': {'type': 'integer', 'minimum': 1, 'maximum': 50},
        'rrf_k': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
        'title_navigation': {'type': 'boolean'},
    }
    schemas = {
        'knowledge.context.policy': config_schema(policy_properties),
        'knowledge.budget': config_schema({'max_calls': POSITIVE, 'max_tokens': POSITIVE}),
        'knowledge.model': config_schema({'route': TEXT}),
        'knowledge.model.route': config_schema({'route': TEXT}),
        'knowledge.gateway': config_schema({'provider_id': TEXT}),
        'knowledge.reranker': config_schema({'route': TEXT}),
        'knowledge.executor': config_schema({'max_attempts': {'type': 'integer', 'minimum': 1, 'maximum': 3}}),
        'knowledge.semantic': config_schema({'config_path': TEXT}),
        'knowledge.service': config_schema({'retrieval_config': config_schema(retrieval_properties)},
                                            required=('retrieval_config',)),
    }
    checks = {'knowledge.context.policy': lambda config: ContextPolicy(**config),
              'knowledge.service': lambda config: PipelineConfig.parse(config['retrieval_config']),
              'knowledge.semantic': lambda config: semantic_inputs(config)}
    return configured_plugins((
        PluginSpec('knowledge.resources', ('knowledge.platform', 'knowledge.research'), ('platform', 'research'), resources),
        PluginSpec('knowledge.context.policy', ('knowledge.context.policy',), (), policy),
        PluginSpec('knowledge.context', ('knowledge.context',), ('knowledge.context.policy',), context),
        PluginSpec('knowledge.budget', ('knowledge.budget',), (), budget),
        PluginSpec('knowledge.model.route', ('knowledge.model', 'llm'),
                   ('models', 'models.route.generation', 'knowledge.context.policy', 'knowledge.budget'), model),
        PluginSpec('knowledge.model', ('knowledge.model', 'llm'),
                   ('models', 'knowledge.context.policy', 'knowledge.budget'), model),
        PluginSpec('knowledge.sessions', ('knowledge.store',), ('session.store',), store),
        PluginSpec('knowledge.workflow.graph', ('knowledge.workflow',), (), workflow),
        PluginSpec('knowledge.driver.graph', ('loop',), (), driver),
        PluginSpec('knowledge.gateway', ('knowledge.gateway',),
                   ('knowledge.platform', 'knowledge.research', 'knowledge.model', 'knowledge.reranker'), gateway),
        PluginSpec('knowledge.reranker', ('knowledge.reranker',),
                   ('models', 'knowledge.model'), reranker),
        PluginSpec('knowledge.reranker.none', ('knowledge.reranker',), (), no_reranker),
        PluginSpec('knowledge.executor', ('knowledge.executor',), (), executor),
        PluginSpec('knowledge.semantic', ('knowledge.semantic',),
                   ('knowledge.platform', 'knowledge.gateway'), semantic),
        PluginSpec('knowledge.service', ('knowledge',),
                   ('knowledge.platform', 'knowledge.research', 'knowledge.model', 'knowledge.store',
                    'knowledge.context.policy', 'knowledge.context', 'runtime', 'knowledge.gateway',
                    'knowledge.executor', 'knowledge.semantic', 'knowledge.workflow',
                    'knowledge.tool_plugins'), knowledge),
        PluginSpec('knowledge.tools.catalog', ('knowledge.tool_plugins',), (), tool_catalog),
        PluginSpec('knowledge.tools', ('knowledge.tools',), ('knowledge',), tools),
        PluginSpec('knowledge.application', ('application',), ('knowledge', 'knowledge.tools', 'loop'), application),
    ), schemas, checks, {'knowledge.semantic': semantic_inputs})


def create_knowledge_application(platform, research, *, profile=None, service_factory=None,
                                 extra_plugins=(), environment=None, models=None):
    from .application_composition import create_borrowed_knowledge

    return create_borrowed_knowledge(platform, research, profile=profile, service_factory=service_factory,
                                    extra_plugins=extra_plugins, environment=environment, models=models)
