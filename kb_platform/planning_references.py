from copy import deepcopy


REFERENCE_FIELDS = frozenset({'citation_id', 'citation_ids', 'evidence_id', 'evidence_ids'})


def project_references(value, mapping, field=None):
    if isinstance(value, dict):
        return {key: project_references(child, mapping, key) for key, child in value.items()}
    if isinstance(value, list):
        return [project_references(child, mapping, field) for child in value]
    if field in REFERENCE_FIELDS and isinstance(value, str):
        return mapping.get(value, value)
    return deepcopy(value)


def planning_bindings(identities):
    return {identity: f'E{number}' for number, identity in enumerate(sorted(set(identities)), start=1)}


def bind_planning_schema(schema, mapping):
    result = deepcopy(schema)
    citations = result['properties']['citation_ids']
    if mapping:
        citations['items'] = {'type': 'string', 'enum': list(mapping.values())}
    return result


def resolve_planning_decision(value, mapping):
    result = deepcopy(value)
    reverse = {alias: identity for identity, alias in mapping.items()}
    if 'citation_ids' in result:
        if any(reference not in reverse for reference in result['citation_ids']):
            raise ValueError('UNKNOWN_PLANNING_REFERENCE')
        result['citation_ids'] = [reverse[reference] for reference in result['citation_ids']]
    return result
