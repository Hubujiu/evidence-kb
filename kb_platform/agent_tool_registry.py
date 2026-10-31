from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from .agent_protocol import ADVANCED_PROPERTIES, DECISION_SCHEMA


@dataclass(frozen=True)
class ToolSpec:
    arguments: Literal['query', 'read', 'references', 'table', 'join', 'expression']
    feature: str | None = None
    retry_safe: bool = True
    handler: str = "advanced"
    mutates_data: bool = False
    incurs_cost: bool = False


TOOLS = {
    'search': ToolSpec('query', handler='search'),
    'read': ToolSpec('read', handler='read'),
    'read_history': ToolSpec('query', handler='history'),
    'calculator': ToolSpec('expression', handler='calculate'),
    'search_pipeline': ToolSpec('query', 'advanced_tools', False, incurs_cost=True),
    'search_tables': ToolSpec('query', 'advanced_tools'),
    'table': ToolSpec('table', 'advanced_tools'),
    'table_join': ToolSpec('join', 'advanced_tools'),
    'navigate_references': ToolSpec('references', 'advanced_tools'),
    'workflow_next': ToolSpec('references', 'advanced_tools'),
    'search_pages': ToolSpec('query', 'page_navigation', handler='search'),
    'search_semantic_pages': ToolSpec('query', 'semantic_pages', False, handler='search', incurs_cost=True),
}


def advanced_tools(config, gateway):
    return {name for name, spec in TOOLS.items()
            if spec.feature == 'advanced_tools' and config.get(spec.feature) and hasattr(gateway, name)}


def decision_schema(config, advanced, semantic_enabled):
    schema = deepcopy(DECISION_SCHEMA)
    actions = schema['properties']['action']['enum']
    if advanced or config.get('explicit_completion'):
        actions.extend(sorted(advanced))
        schema['properties']['intent']['enum'].append('out_of_domain')
        schema['properties'].update(deepcopy(ADVANCED_PROPERTIES))
    if config.get('page_navigation'):
        actions.append('search_pages')
    if semantic_enabled:
        actions.append('search_semantic_pages')
    return schema


def tool_arguments(action, decision, message, config):
    spec = TOOLS[action]
    if spec.arguments == 'query':
        args = {'query': decision.get('query') if config['rewrite'] else message}
        if action == 'read_history' and config.get('expand_history'):
            args.update(offset=decision.get('offset', 0), event_seq=decision.get('event_seq'))
        return args
    if spec.arguments == 'expression':
        return {'expression': decision.get('expression', '')}
    args = {'citation_ids': decision.get('citation_ids', [])}
    if spec.arguments == 'read':
        args['offset'] = decision.get('offset', 0)
    elif spec.arguments == 'table':
        args.update(operation=decision.get('table_operation', 'rows'), column=decision.get('column'),
                    filters=decision.get('filters', []))
    elif spec.arguments == 'join':
        args.update(left_key=decision['left_key'], right_key=decision['right_key'],
                    how=decision.get('join_how', 'inner'))
    return args
