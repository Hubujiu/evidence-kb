"""Explicit, per-service tool registration; no ambient imports or global mutation."""
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
import re
from types import MappingProxyType

from .agent_schema import compile_schema, validate_output


@dataclass(frozen=True)
class ToolContext:
    actor: object
    session_id: str
    turn_id: str
    call_id: str = field(default='', kw_only=True)


@dataclass(frozen=True)
class ToolResult:
    data: dict


@dataclass(frozen=True)
class ToolPlugin:
    name: str
    description: str
    parameters: dict
    execute: Callable[[ToolContext, dict], ToolResult]
    version: str = '1'
    retry_safe: bool = False
    mutates_data: bool = False
    incurs_cost: bool = False
    direct_return: bool = False


class ToolPlugins:
    def __init__(self, plugins: Iterable[ToolPlugin] = (), *, reserved_names=()):
        registered, validators = {}, {}
        for plugin in plugins:
            if (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', plugin.name)
                    or plugin.name in reserved_names or plugin.name in registered):
                raise ValueError('INVALID_OR_DUPLICATE_TOOL_PLUGIN')
            if not plugin.version or not plugin.description or not callable(plugin.execute):
                raise ValueError('INVALID_TOOL_PLUGIN')
            if plugin.retry_safe and (plugin.mutates_data or plugin.incurs_cost):
                raise ValueError('SIDE_EFFECTING_PLUGIN_CANNOT_RETRY')
            if plugin.parameters.get('type') != 'object':
                raise ValueError('PLUGIN_PARAMETERS_MUST_BE_OBJECT')
            plugin = ToolPlugin(**{**plugin.__dict__, 'parameters': deepcopy(plugin.parameters)})
            validators[plugin.name] = compile_schema(plugin.parameters)
            registered[plugin.name] = plugin
        self._plugins = MappingProxyType(registered)
        self._validators = MappingProxyType(validators)

    def __contains__(self, name):
        return name in self._plugins

    def __getitem__(self, name):
        plugin = self._plugins[name]
        return ToolPlugin(**{**plugin.__dict__, 'parameters': deepcopy(plugin.parameters)})

    def names(self):
        return list(self._plugins)

    def bind(self, plugins):
        """Build an isolated invocation catalog without mutating shared tools."""
        return type(self)(plugins)

    def manifest(self):
        return [{key: deepcopy(value) for key, value in plugin.__dict__.items() if key != 'execute'}
                for plugin in self._plugins.values()]

    def execute(self, name, context, args):
        validate_output(args, self._validators[name])
        result = self._plugins[name].execute(context, deepcopy(args))
        if not isinstance(result, ToolResult) or not isinstance(result.data, dict):
            raise ValueError('INVALID_PLUGIN_RESULT')
        return result
