from .agent_decisions import Answer, ToolCall
from .agent_plugins import ToolContext
from .harness_services import builtin_plugins, model_tools_profile
from .plugin_host import PluginHost

__all__ = ['Answer', 'ToolCall', 'ToolAgent']


class ToolAgent:
    def __init__(self, decide, plugins=(), *, runtime=None, profile=None, extra_plugins=()):
        self.profile = profile if profile is not None else model_tools_profile()
        self.catalog = (*builtin_plugins(runtime=runtime, decide=decide, tools=plugins), *extra_plugins)

    def run(self, message, context: ToolContext, *, max_operations=128, deadline_seconds=900,
            cancellation=None, before_operation=None):
        with PluginHost(self.profile, self.catalog) as host:
            return host.call('loop', 'run', message, context, max_operations=max_operations,
                deadline_seconds=deadline_seconds, cancellation=cancellation, before_operation=before_operation)
