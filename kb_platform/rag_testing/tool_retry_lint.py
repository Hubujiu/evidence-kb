import ast


OWNER = 'kb_platform/provider_runtime.py'
CALLER = 'kb_platform/knowledge_planning.py'


def tool_retry_source_violations(source, caller):
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ToolExecutor')
    execute = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == 'execute')
    nested = {node.name: node for node in execute.body if isinstance(node, ast.FunctionDef)}
    errors = []
    stop = nested.get('stop')
    expected = [
        'not read_only',
        'not classify_failure(retry_state.outcome.exception()).retryable',
        'stop_after_attempt(self.max_attempts)(retry_state)',
        'retry_state.idle_for + retry_state.upcoming_sleep > self.max_wait_seconds',
        'deadline_seconds is not None and time.monotonic() - started + retry_state.upcoming_sleep >= deadline_seconds',
    ]
    if (stop is None or len(stop.body) != 1 or not isinstance(stop.body[0], ast.Return)
            or not isinstance(stop.body[0].value, ast.BoolOp) or not isinstance(stop.body[0].value.op, ast.Or)
            or [ast.unparse(item) for item in stop.body[0].value.values] != expected):
        errors.append('owned stop must independently enforce read-only/classification and all attempt/wait/deadline caps')
    wait = ast.unparse(nested['wait']) if 'wait' in nested else ''
    for fragment in ("hint = getattr(exc, 'retry_after_seconds', None)",
                     'hint = float(hint) if type(hint) in (int, float) else None',
                     'except OverflowError:', 'hint = None',
                     'if hint is not None and math.isfinite(hint) and (hint >= 0):\n        delay = max(delay, hint)',
                     "retry_after_seconds(cause.response.headers.get('Retry-After'))", 'return delay'):
        if fragment not in wait:
            errors.append('wait must preserve finite nonnegative public and HTTP retry hints without clamping')
            break
    text = ast.unparse(execute)
    lock = next((node for node in execute.body if isinstance(node, ast.With)), None)
    claim = ast.unparse(lock) if lock else ''
    for fragment in ("if existing['fingerprint'] != fingerprint:",
                     "raise RuntimePolicyError('TOOL_REQUEST_ID_REUSED')",
                     "if existing['outcome'] is None:", "raise RuntimePolicyError('TOOL_REQUEST_IN_PROGRESS')",
                     "asdict(deepcopy(existing['outcome']))", "'reused': True",
                     "self._receipts[request_id] = {'fingerprint': fingerprint, 'outcome': None}"):
        if fragment not in claim:
            errors.append('claim, fingerprint and in-progress checks plus isolated terminal reuse must stay under the lock')
            break
    invoke = ast.unparse(nested['invoke']) if 'invoke' in nested else ''
    if ('if deadline_seconds is not None and time.monotonic() - started >= deadline_seconds:\n        return (False, None)' not in invoke
            or 'return (True, function())' not in invoke or invoke.count('function()') != 1
            or "ToolOutcome('ERROR', None, attempt - 1, 'deadline_exceeded'" not in text):
        errors.append('actual attempts must exclude deadline refusals and call the operation only once per invoke')
    outer = next((node for node in execute.body if isinstance(node, ast.Try)), None)
    if (outer is None or not outer.finalbody or
            "self._receipts[request_id]['outcome'] = deepcopy(outcome)" not in ast.unparse(outer.finalbody[0])
            or "ToolOutcome('UNKNOWN', None, attempt, 'outcome_unknown', 'TOOL_EXECUTION_INTERRUPTED')" not in text
            or not any(isinstance(node, ast.If) and ast.unparse(node.test) == 'sleep_interrupted'
                       and len(node.body) == 1 and isinstance(node.body[0], ast.Raise) for node in ast.walk(execute))
            or 'reraise=True' not in text):
        errors.append('interruptions must remain unknown, propagate, and publish the final receipt without replay')
    planning = ast.parse(caller)
    invoke_tool = next(node for node in ast.walk(planning) if isinstance(node, ast.FunctionDef) and node.name == 'invoke_tool')
    if "self.session_id + ':' + self.request_id + ':' + identity" not in ast.unparse(invoke_tool):
        errors.append('the real planning caller must retain session/turn/tool identity namespacing')
    return errors
