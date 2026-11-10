import math
from types import SimpleNamespace


IDENTITIES = ('request_id', 'physical_request_id', 'provider_request_id', 'operation_id',
    'budget_operation_id', 'admission_receipt_id')
COUNTERS = frozenset({'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens',
    'output_tokens', 'requests', 'cached_tokens', 'reasoning_tokens'})
FACTS = {
    'outcome': frozenset({'UNKNOWN', 'COMMITTED', 'NOT_COMMITTED'}),
    'capture_state': frozenset({'COMMITTED', 'UNCONFIRMED', 'FAILED'}),
    'ledger_settlement': frozenset({'COMMITTED', 'UNCONFIRMED', 'NOT_ATTEMPTED'})}
PUBLIC_FACT_PROPERTIES = {**{key: {'type': 'string', 'minLength': 1, 'maxLength': 200} for key in IDENTITIES},
    **{key: {'enum': sorted(values)} for key, values in FACTS.items()},
    'model_sent': {'type': 'boolean'}, 'provider_http_status': {'type': 'integer', 'minimum': 100, 'maximum': 599},
    'retry_after_seconds': {'type': 'number', 'minimum': 0},
    'observed_usage': {'type': 'object', 'properties': {key: {'type': 'number', 'minimum': 0} for key in COUNTERS},
        'additionalProperties': False},
    'cache_kind': {'type': 'string', 'maxLength': 40}, 'attempts': {'type': 'integer', 'minimum': 0},
    'recovery_action': {'enum': ['none', 'inspect_operation', 'inspect_request', 'wait', 'fix_request']}}
PUBLIC_MODEL_FACTS = {'type': 'object', 'properties': PUBLIC_FACT_PROPERTIES,
    'required': ['recovery_action'], 'additionalProperties': False}
PUBLIC_ERROR_SCHEMA = {'type': 'object', 'properties': {**PUBLIC_FACT_PROPERTIES,
    'error': {'type': 'string'}, 'detail': {'type': 'string'}},
    'required': ['error', 'detail', 'recovery_action'], 'additionalProperties': False}


def public_error(error, *, code=None, message=None):
    observed = {}
    current, visited = error, set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        details = getattr(current, 'details', {})
        if isinstance(details, dict):
            for key, value in details.items():
                observed.setdefault(key, value)
        for key in (*IDENTITIES, *FACTS, 'model_sent', 'observed_usage', 'status', 'retry_after_seconds'):
            value = getattr(current, key, None)
            if value is not None:
                observed.setdefault(key, value)
        current = getattr(current, '__cause__', None)
    result = {'error': code or getattr(error, 'code', 'INTERNAL_ERROR'),
        'detail': str(error) if message is None else message}
    for key in IDENTITIES:
        value = observed.get(key)
        if isinstance(value, str) and 0 < len(value) <= 200 and all(ord(char) >= 32 for char in value):
            result[key] = value
    for key, permitted in FACTS.items():
        value = observed.get(key)
        if isinstance(value, str) and value in permitted:
            result[key] = value
    if type(observed.get('model_sent')) is bool:
        result['model_sent'] = observed['model_sent']
    http_code = observed.get('status')
    if type(http_code) is int and 100 <= http_code <= 599:
        result['provider_http_status'] = http_code
    delay = observed.get('retry_after_seconds')
    if type(delay) in (int, float) and math.isfinite(delay) and delay >= 0:
        result['retry_after_seconds'] = delay
    usage = observed.get('observed_usage')
    if isinstance(usage, dict):
        result['observed_usage'] = {key: value for key, value in usage.items()
            if key in COUNTERS and type(value) in (int, float) and math.isfinite(value) and value >= 0}
    if result.get('operation_id') or result.get('budget_operation_id'):
        action = 'inspect_operation'
    elif result.get('model_sent') is True or result.get('outcome') == 'UNKNOWN':
        action = 'inspect_request'
    elif result.get('retry_after_seconds') is not None:
        action = 'wait'
    else:
        action = 'fix_request'
    result['recovery_action'] = action
    return result


def public_error_headers(payload):
    delay = payload.get('retry_after_seconds')
    return {} if delay is None else {'Retry-After': str(math.ceil(delay))}


def public_model_facts(details):
    observed = dict(details)
    usage = observed.get('usage')
    if 'observed_usage' not in observed and isinstance(usage, dict) and usage.get('available') is True:
        observed['observed_usage'] = usage
    payload = public_error(SimpleNamespace(details=observed), code='MODEL_CALL_FACTS', message='')
    result = {key: value for key, value in payload.items() if key not in {'error', 'detail'}}
    if (not details.get('error') and not details.get('code')
            and details.get('ledger_settlement') != 'UNCONFIRMED' and details.get('capture_state') != 'FAILED'):
        result['recovery_action'] = 'none'
    kind = details.get('cache_kind')
    if isinstance(kind, str) and 0 < len(kind) <= 40:
        result['cache_kind'] = kind
    if type(details.get('attempts')) is int and details['attempts'] >= 0:
        result['attempts'] = details['attempts']
    return result
