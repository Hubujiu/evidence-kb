from __future__ import annotations

import math

from .core import PlatformError


HEADERS = ('from_step', 'to_step', 'condition_key', 'condition_value')


def next_step(table, current_step=None, facts=None):
    if (not isinstance(table, dict) or table.get('headers') != list(HEADERS) or
            not isinstance(table.get('rows'), list) or len(table['rows']) > 1000):
        raise PlatformError('WORKFLOW_STRUCTURE_UNAVAILABLE',
                            'Provide an original typed workflow table with explicit step and condition columns', 422)
    facts = {} if facts is None else facts
    if (not isinstance(facts, dict) or len(facts) > 20 or any(
            not isinstance(key, str) or not key or len(key) > 100 or
            type(value) not in (str, bool, int, float) or isinstance(value, str) and len(value) > 500 or
            isinstance(value, float) and not math.isfinite(value) for key, value in facts.items())):
        raise PlatformError('INVALID_WORKFLOW_STATE', 'Provide bounded exact facts for workflow conditions', 422)
    edges = []
    row_numbers = table.get('source_row_numbers', list(range(2, len(table['rows']) + 2)))
    if not isinstance(row_numbers, list) or len(row_numbers) != len(table['rows']):
        raise PlatformError('INVALID_WORKFLOW_STRUCTURE', 'Original row coordinates are missing', 422)
    for number, row in zip(row_numbers, table['rows'], strict=True):
        if (not isinstance(row, list) or len(row) != 4 or
                any(not isinstance(row[index], str) or not row[index].strip() or len(row[index]) > 200
                    for index in (0, 1)) or not isinstance(row[2], str) or len(row[2]) > 100 or
                type(row[3]) not in (str, bool, int, float, type(None)) or
                isinstance(row[3], str) and len(row[3]) > 500 or
                isinstance(row[3], float) and not math.isfinite(row[3]) or
                not row[2] and row[3] not in ('', None)):
            raise PlatformError('INVALID_WORKFLOW_STRUCTURE', 'Workflow edge or condition is invalid', 422)
        edges.append({'from_step': row[0], 'to_step': row[1], 'condition_key': row[2],
                      'condition_value': row[3], 'source_row_number': number})
    if current_step is None or current_step == '':
        return {'status': 'clarify', 'missing_info': ['current_step'], 'next_steps': [], 'edges': []}
    if not isinstance(current_step, str) or len(current_step) > 200:
        raise PlatformError('INVALID_WORKFLOW_STATE', 'A bounded current step identifier is required', 422)
    outgoing = [edge for edge in edges if edge['from_step'] == current_step]
    if not outgoing:
        return {'status': 'insufficient_evidence', 'missing_info': ['outgoing_edge_for_current_step'],
                'next_steps': [], 'edges': []}
    unknown = sorted({edge['condition_key'] for edge in outgoing
                      if edge['condition_key'] and edge['condition_key'] not in facts})
    if unknown:
        return {'status': 'clarify', 'missing_info': unknown, 'next_steps': [], 'edges': outgoing}
    matched = [edge for edge in outgoing if not edge['condition_key'] or
               type(facts[edge['condition_key']]) is type(edge['condition_value']) and
               facts[edge['condition_key']] == edge['condition_value']]
    if not matched:
        return {'status': 'insufficient_evidence', 'missing_info': ['supported_branch_for_current_facts'],
                'next_steps': [], 'edges': outgoing}
    if len(matched) != 1:
        return {'status': 'ambiguous', 'missing_info': ['unique_source_supported_transition'],
                'next_steps': [], 'edges': matched}
    target = matched[0]['to_step']
    return {'status': 'complete' if target == '__END__' else 'ready', 'missing_info': [],
            'next_steps': [] if target == '__END__' else [target], 'edges': matched}
