from copy import deepcopy

from .agent_context import canonical


def project_tool_results(results, *, budget_bytes, role):

    ordinary = sum(item['name'] != 'read_history' for item in results)
    per_result = max(256, budget_bytes // max(1, ordinary))
    projected = []
    for original in results:
        item = deepcopy(original)
        if (role in {'answer', 'verifier'} and item['name'] == 'search_pipeline'
                and isinstance(item.get('result'), dict)):
            item['result'].pop('coverage', None)
        if item['name'] == 'read_history' or len(canonical(item).encode('utf-8')) <= per_result:
            projected.append(item)
            continue
        view = {key: deepcopy(item[key]) for key in ('name', 'status', 'artifact_id') if key in item}
        view['result'] = {'truncated': True, 'complete_result': False,
                          'externalized': True,
                          'note': 'Full result is stored in the artifact. Omitted values are not evidence.'}
        result = item.get('result') or {}

        for key in ('coverage', 'evidence_ids', 'value', 'count', 'operation', 'status', 'missing_info'):
            if key in result:
                candidate = deepcopy(view)
                candidate['result'][key] = result[key]
                if len(canonical(candidate).encode('utf-8')) <= per_result:
                    view = candidate
        projected.append(view)
    return projected
