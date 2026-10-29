"""Explicit builtin catalog. Profiles choose services; dependencies govern startup."""
from copy import deepcopy

from .agent_decisions import Answer, ToolCall
from .agent_engine import Runtime
from .agent_plugins import ToolPlugins
from .harness_loop import NativeLoop
from .harness_sessions import MemorySessions
from .plugin_host import PluginSpec


class HistoryPrompt:
    def prepare(self, messages, tools, context):
        return {'messages': deepcopy(messages), 'tools': deepcopy(tools)}


class CallableModel:
    def __init__(self, decide):
        if not callable(decide):
            raise ValueError('MODEL_ADAPTER_REQUIRED')
        self._decide = decide

    def decide(self, request, context):
        decision = self._decide(request['messages'], request['tools'])
        if isinstance(decision, Answer):
            return {'kind': 'answer', 'value': decision.value}
        if isinstance(decision, ToolCall):
            return {'kind': 'tool', 'name': decision.name, 'arguments': decision.arguments}
        raise ValueError('INVALID_MODEL_DECISION')


def _config(config, allowed=()):
    if set(config) - set(allowed):
        raise ValueError('UNKNOWN_HARNESS_PLUGIN_CONFIG')


def builtin_plugins(*, runtime=None, decide=None, tools=()):
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
        ctx.provide('model', CallableModel(decide))

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
