from copy import deepcopy
from dataclasses import dataclass, field

from .agent_plugins import ToolContext, ToolPlugins, ToolResult
from .agent_schema import validate_output
from .agent_tool_registry import TOOLS


@dataclass(frozen=True)
class KnowledgeToolContext(ToolContext):
    scope_id: str
    source_policy: dict


@dataclass(frozen=True)
class KnowledgeToolResult(ToolResult):
    evidence: list[dict] = field(default_factory=list)


class KnowledgeToolPlugins(ToolPlugins):
    def __init__(self, plugins=()):
        super().__init__(plugins, reserved_names=(*TOOLS, "answer", "clarify"))

    def schema(self, base):
        if not self.names():
            return base
        schema = deepcopy(base)
        schema["properties"]["action"]["enum"].extend(self.names())
        schema["properties"]["plugin_args"] = {
            "anyOf": [deepcopy(self[name].parameters) for name in self.names()]
        }
        return schema

    def instructions(self):
        return "\n".join(f"{name}(plugin_args): {self[name].description}" for name in self.names())

    def validate_arguments(self, name, arguments):
        validate_output(arguments, self._validators[name])

    def arguments(self, decision):
        return deepcopy(decision.get("plugin_args", {}))

    def execute(self, name, context, args):
        result = super().execute(name, context, args)
        if isinstance(result, KnowledgeToolResult):
            if not isinstance(result.evidence, list):
                raise ValueError("INVALID_PLUGIN_RESULT")
            return result
        return KnowledgeToolResult(data=result.data)
