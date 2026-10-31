from copy import deepcopy

from .agent_engine import Runtime
from .agent_plugins import ToolPlugins
from .harness_loop import NativeLoop
from .harness_sessions import MemorySessions, DurableSessions
from .harness_application import AgentApplication
from .harness_context import HistoryContext, RecentTurns
from .harness_model import CallableModel, StructuredDecisionModel
from .context_budget import ContextBudget
from .session_store import SessionStore
from .plugin_host import PluginSpec


class HistoryPrompt:
    def prepare(self, messages, tools, context):
        return {'messages': deepcopy(messages), 'tools': deepcopy(tools)}


def _config(config, allowed=()):
    if set(config) - set(allowed):
        raise ValueError('UNKNOWN_HARNESS_PLUGIN_CONFIG')


def builtin_plugins(*, runtime=None, decide=None, model=None, tools=()):
    registered = tuple(tools)

    def runtime_plugin(ctx, config):
        _config(config)
        ctx.provide('runtime', runtime if runtime is not None else Runtime())

    def sessions_plugin(ctx, config):
        _config(config)
        ctx.provide('sessions', MemorySessions())

    def prompt_plugin(ctx, config):
        _config(config)
        ctx.provide('prompt', HistoryPrompt())

    def tools_plugin(ctx, config):
        _config(config)
        ctx.provide('tools', ToolPlugins(registered))

    def model_plugin(ctx, config):
        _config(config)
        ctx.provide('model', model if model is not None else CallableModel(decide))

    def store_plugin(ctx, config):
        _config(config, ('root',))
        ctx.provide('session.store', SessionStore(**config))

    def durable_sessions(ctx, config):
        _config(config)
        ctx.provide('sessions', DurableSessions(ctx.require('session.store')))

    def context_policy(ctx, config):
        _config(config, ('input_budget', 'output_reserve', 'model_window', 'safety_reserve', 'strict_tokens'))
        ctx.provide('context.policy', ContextBudget(**config))

    def compaction_plugin(ctx, config):
        _config(config)
        ctx.provide('compaction', RecentTurns())

    def context_plugin(ctx, config):
        _config(config, ('system',))
        selected_model = ctx.require('model')
        policy = ctx.require('context.policy')
        model_policy = getattr(selected_model, 'policy', None)
        if model_policy is not None:
            policy = ContextBudget(
                input_budget=min(policy.effective_input_budget, model_policy.effective_input_budget),
                output_reserve=model_policy.output_reserve, model_window=model_policy.model_window,
                safety_reserve=model_policy.safety_reserve,
                strict_tokens=policy.strict_tokens or model_policy.strict_tokens)
        ctx.provide('prompt', HistoryContext(policy=policy,
            compaction=ctx.require('compaction'), measure=getattr(selected_model, 'estimate_request', None),
            **config))

    def structured_model(ctx, config):
        _config(config, ('output_limit',))
        if model is None:
            raise ValueError('MODEL_ADAPTER_REQUIRED')
        ctx.provide('model', StructuredDecisionModel(model, **config))

    def service_model(ctx, config):
        _config(config, ('output_limit',))
        ctx.provide('model', StructuredDecisionModel(ctx.require('llm'), **config))

    def application_plugin(ctx, config):
        _config(config)
        ctx.provide('application', AgentApplication(ctx.require('sessions'), ctx.require('loop')))

    def mount_loop(mode):
        def mount(ctx, config):
            _config(config, ('mode',))
            if config.get('mode', mode) != mode:
                raise ValueError('LOOP_PLUGIN_MODE_MISMATCH')
            ctx.provide('loop', NativeLoop(ctx.host, ctx.require('runtime'), ctx.require('sessions'),
                ctx.require('prompt') if mode == 'model-tools' else None, ctx.require('tools'), mode=mode,
                model=ctx.require('model') if mode == 'model-tools' else None))
        return mount

    dependencies = ('runtime', 'sessions', 'tools')
    return (
        PluginSpec('harness.store.sqlite', ('session.store',), (), store_plugin),
        PluginSpec('harness.sessions.durable', ('sessions',), ('session.store',), durable_sessions),
        PluginSpec('harness.context.policy', ('context.policy',), (), context_policy),
        PluginSpec('harness.compaction.recent', ('compaction',), (), compaction_plugin),
        PluginSpec('harness.context.history', ('prompt',), ('context.policy', 'compaction', 'model'), context_plugin),
        PluginSpec('harness.model.service', ('model',), ('llm',), service_model),
        PluginSpec('harness.model.structured', ('model',), (), structured_model),
        PluginSpec('harness.application', ('application',), ('sessions', 'loop'), application_plugin),
        PluginSpec('harness.runtime', ('runtime',), (), runtime_plugin),
        PluginSpec('harness.sessions.memory', ('sessions',), (), sessions_plugin),
        PluginSpec('harness.prompt.history', ('prompt',), (), prompt_plugin),
        PluginSpec('harness.tools', ('tools',), (), tools_plugin),
        PluginSpec('harness.model.callable', ('model',), (), model_plugin),
        PluginSpec('harness.loop', ('loop',), dependencies, mount_loop('single-tool')),
        PluginSpec('harness.loop.model-tools', ('loop',), (*dependencies, 'prompt', 'model'), mount_loop('model-tools')),
    )


def model_tools_profile():
    return {'version': 1, 'plugins': [
        {'id': 'runtime', 'use': 'harness.runtime'},
        {'id': 'sessions', 'use': 'harness.sessions.memory'},
        {'id': 'prompt', 'use': 'harness.prompt.history'},
        {'id': 'tools', 'use': 'harness.tools'},
        {'id': 'model', 'use': 'harness.model.callable'},
        {'id': 'loop', 'use': 'harness.loop.model-tools'},
    ]}
