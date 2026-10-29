"""Bounded model views; full tool results remain in the existing artifact store."""
from copy import deepcopy

from .agent_context import canonical


def project_tool_results(results, *, budget_bytes):
    """History text uses ContextManager's own windowing and quote validation."""
    ordinary = sum(item['name'] != 'read_history' for item in results)
    per_result = max(256, budget_bytes // max(1, ordinary))
    projected = []
    for item in results:
        if item['name'] == 'read_history' or len(canonical(item).encode('utf-8')) <= per_result:
            projected.append(deepcopy(item))
            continue
        view = {key: deepcopy(item[key]) for key in ('name', 'status', 'artifact_id') if key in item}
        view['result'] = {'truncated': True, 'complete_result': False,
                          'externalized': True,
                          'note': 'Full result is stored in the artifact. Omitted values are not evidence.'}
        result = item.get('result') or {}
        # Preserve only whole values that fit; never split a numeric value, quote or row.
        for key in ('evidence_ids', 'value', 'count', 'operation', 'status', 'missing_info'):
            if key in result:
                candidate = deepcopy(view)
                candidate['result'][key] = result[key]
                if len(canonical(candidate).encode('utf-8')) <= per_result:
                    view = candidate
        projected.append(view)
    return projected
