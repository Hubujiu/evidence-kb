import re


class RemoteModelError(RuntimeError):
    def __init__(self, code: str, status: int | None = None, *, retry_after_seconds=None, model_sent=None):
        self.code, self.status = code, status
        self.retry_after_seconds = retry_after_seconds
        if model_sent is not None:
            self.model_sent = model_sent
        super().__init__(code + (f" (HTTP {status})" if status else ""))


class TerminalContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ModelPreflightError(ValueError):
    model_sent = False

    def __init__(self, code):
        self.code = self.preflight_code = code
        super().__init__(code)


ERROR_FIELDS = ('status', 'retry_after_seconds', 'model_sent', 'physical_request_id',
                'request_id', 'call_record', 'preflight_code', 'output_diagnostics', 'observed_usage', 'trace',
                'operation_id', 'budget_operation_id', 'admission_receipt_id', 'known_zero_send',
                'provider_request_id', 'ledger_settlement', 'capture_state')
UNOBSERVED_RESULT = object()


def preserve_result_failure(error, usage, *, result=None, physical_request_id=None):
    error.model_sent = True
    error.preflight_code = None
    if isinstance(usage, dict):
        error.observed_usage = dict(usage)
    request_id = getattr(result, 'request_id', None)
    if isinstance(request_id, str) and request_id:
        error.request_id = request_id
    physical_request_id = getattr(result, 'physical_request_id', None) or physical_request_id
    if isinstance(physical_request_id, str) and physical_request_id:
        error.physical_request_id = physical_request_id


def error_details(error, *, dispatched=None, result=UNOBSERVED_RESULT, trace=None):
    cause = getattr(error, '__cause__', None)
    details = {key: getattr(error, key, getattr(cause, key, None)) for key in ERROR_FIELDS}
    code = getattr(error, 'code', None)
    details['code'] = code if isinstance(code, str) else (
        str(error) if re.fullmatch('[A-Z][A-Z0-9_]{0,99}', str(error)) else 'MODEL_EXECUTION_FAILED')
    if isinstance(error, ModelPreflightError) and details['model_sent'] is not True:
        details.update(preflight_code=details['preflight_code'] or error.code, model_sent=False)
    if result is not UNOBSERVED_RESULT:
        details['model_sent'] = True
        observed = result if isinstance(result, dict) else {
            'usage': getattr(result, 'usage', None), 'request_id': getattr(result, 'request_id', None),
            'physical_request_id': getattr(result, 'physical_request_id', None)}
        if isinstance(observed.get('usage'), dict):
            details['observed_usage'] = observed['usage']
        if details['physical_request_id'] is None:
            details['physical_request_id'] = observed.get('physical_request_id')
        if details['request_id'] is None:
            details['request_id'] = observed.get('request_id')
    elif details['model_sent'] is None and dispatched is False:
        details['model_sent'] = False
    if details['model_sent'] is True:
        details['preflight_code'] = None
    if trace is not None:
        details['trace'] = trace
        if details['physical_request_id'] is None and isinstance(trace.get('last_physical_request_id'), str):
            details['physical_request_id'] = trace['last_physical_request_id']
    if isinstance(error, TerminalContractError):
        details['terminal'] = True
    return details


def model_error(details):
    if details.get('terminal'):
        error = TerminalContractError(details['code'])
    elif details.get('preflight_code') and details.get('model_sent') is not True:
        error = ModelPreflightError(details['code'])
    else:
        error = RemoteModelError(details['code'], details.get('status'),
                                 retry_after_seconds=details.get('retry_after_seconds'))
    for key in ERROR_FIELDS:
        if details.get(key) is not None:
            setattr(error, key, details[key])
    return error
