import ast


OWNER = 'kb_platform/remote.py'
SDK = 'kb_platform/luna_sdk.py'


def remote_retry_source_violations(source, sdk_source):
    tree = ast.parse(source)
    methods = {node.name + '.' + method.name: method for node in tree.body if isinstance(node, ast.ClassDef)
               for method in node.body if isinstance(method, ast.FunctionDef)}
    errors = []

    def require(name, fragments, message):
        text = ast.unparse(methods[name]) if name in methods else ''
        if any(fragment not in text for fragment in fragments):
            errors.append(message)
        return text

    luna = require('_Transport._post', [
        'waited = 0.0', "previous_id = trace.get('last_physical_request_id')", 'for attempt in range(self.retries + 1):', "trace['attempt'] += 1",
        "'transport_attempt': attempt + 1", "'retry_of': previous_id, 'retry_reason': previous_reason",
        "previous_reason = 'format_correction' if previous_id and trace.get('format_attempt', 1) > 1 else None",
        'if failure.outcome == \'UNKNOWN\':', "raise RemoteModelError('PROVIDER_OUTCOME_UNKNOWN') from None",
        "if 'physical_request_id' not in metadata:\n                trace['attempt'] -= 1",
    ], 'transport attempts, UNKNOWN refusal and linked format continuation must retain distinct accounting')
    for fragment, count in [('if failure.retryable and attempt < self.retries:', 2),
                            ('if waited + delay > self.max_retry_wait_seconds:', 2),
                            ("metadata.get('deadline_monotonic', math.inf) - time.monotonic() <= delay", 2),
                            ('time.sleep(delay)', 2), ('waited += delay', 2), ('self._send_attempt(', 1), ("trace['attempt'] -= 1", 2)]:
        if luna.count(fragment) != count:
            errors.append('both transport retry paths require classification, attempt/wait/deadline gates before the sole dispatch')
            break
    require('LunaClient._complete', [
        "'attempt': 0, 'format_attempt': 0", 'format_attempts = 2 if json_output and purpose in self.contract_purposes else 1',
        'for format_attempt in range(format_attempts):', "trace['format_attempt'] = format_attempt + 1",
        'if format_attempt + 1 == format_attempts or isinstance(exc, TerminalContractError):',
        "payload = {**payload, 'messages': [{'role': 'system', 'content': system + correction}, payload['messages'][1]]}",
        "self._post('/chat/completions', payload, self._headers, purpose, key, trace=trace)",
    ], 'format correction must retain one logical trace, original user payload, terminal contracts and two-stage cap')
    complete = methods.get('LunaClient._complete')
    if complete:
        loops = [node for node in complete.body if isinstance(node, ast.For) and ast.unparse(node.target) == 'format_attempt']
        if len(loops) != 1 or any(isinstance(node, ast.Assign) and any(
                ast.unparse(target) == "trace['attempt']" for target in node.targets) for node in ast.walk(loops[0])):
            errors.append('format correction must not reset global actual-attempt accounting')
    require('_GoogleRequest.metadata', [
        "'attempt': self.attempt", "'transport_attempt': self.attempt",
        "'auth_refreshes': self.auth_refreshes", "'authentication_retry': self.authentication_retry",
        "'retry_of': self.retry_of", "'retry_reason': self.retry_reason",
    ], 'Google metadata must distinguish completed auth stages from actual model attempts')
    google = require('GoogleEmbeddingProvider._post', [
        'state = trace if trace is not None else _GoogleRequest()', 'while True:',
        'policy.wait(state)', 'state.attempt += 1', 'state.authentication_retry = False',
        "if 'physical_request_id' not in metadata:\n                state.attempt -= 1",
        "if failure.outcome == 'UNKNOWN':", "raise RemoteModelError('PROVIDER_OUTCOME_UNKNOWN') from None",
        "state.retry_of, state.retry_reason = (metadata['physical_request_id'], metadata['http_error_type'])",
    ], 'Google continuation must share state and keep actual attempts, UNKNOWN and physical links separate')
    if (google.count('state.retries_used < self.retries') != 2 or google.count('state.retries_used += 1') != 2
            or google.count('self._send_attempt(') != 1 or 'state.retries_used =' in google):
        errors.append('Google auth continuation must not reset or bypass its shared transport retry budget')
    embed = methods.get('GoogleEmbeddingProvider._embed')
    inner = next((node for node in ast.walk(embed) if isinstance(node, ast.FunctionDef) and node.name == 'embed_one'), None) if embed else None
    inner_text = ast.unparse(inner) if inner else ''
    for fragment in ('with self._vertex_slots:', 'if exc.status != 401:',
                     "headers = self._vertex_headers(purpose, key, headers['Authorization'])\n                trace.auth_refreshes = 1\n                trace.authentication_retry = True",
                     'trace = _GoogleRequest()'):
        if fragment not in inner_text:
            errors.append('one 401 continuation promotes auth stage only after headers return and retains the work slot')
            break
    if (inner is None or inner_text.count('self._post(') != 2 or inner_text.count('trace.auth_refreshes = 1') != 1
            or inner_text.count('self._vertex_slots') != 1
            or any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(inner))):
        errors.append('the per-item auth continuation must not become an unbounded retry loop')
    require('GoogleEmbeddingProvider._vertex_headers', [
        'with self._auth_lock:', "current = 'Bearer ' + (self._credentials.token or '')",
        'if not self._credentials.valid or current == rejected_authorization:',
        'self._credentials.refresh(bounded_request)',
    ], 'credential refresh and current-token adoption must share the auth lock and rejected-token guard')
    policy = methods.get('_GoogleRequestPolicy.wait')
    text = require('_GoogleRequestPolicy.wait', [
        'started = self._clock()', 'delay = max(self._next_send, self._cooldown_until) - now',
        'if delay <= 0:', 'self._next_send = now + self._interval',
        "if state.wait_seconds + now - started + delay > self.settings['max_retry_wait_seconds']:",
        'finally:\n        state.wait_seconds += max(0.0, self._clock() - started)',
    ], 'shared wait must recheck cooldown and budget and account actual elapsed time even on failure')
    if policy and any(isinstance(node, ast.Call) and ast.unparse(node.func) == 'self._sleep'
                      for lock in ast.walk(policy) if isinstance(lock, ast.With) for node in ast.walk(lock)):
        errors.append('shared policy sleep must remain outside the lock')
    if text.count('self._sleep(delay)') != 1:
        errors.append('shared wait must sleep once per rechecked iteration')
    require('_GoogleRequestPolicy.defer', [
        'delay = max(delay, retry_after)', 'with self._lock:',
        'self._cooldown_until = max(self._cooldown_until, self._clock() + delay)',
    ], 'shared cooldown is nondecreasing and Retry-After is not clamped')
    enter = require('_FifoSlots.__enter__', [
        'with self._cond:', 'self._waiters.append(token)',
        'while self._waiters[0] is not token or self._available == 0:', 'except BaseException:',
        'self._waiters.remove(token)', 'self._waiters.popleft()', 'self._available -= 1',
    ], 'FIFO claims and interrupted waiter removal must stay under the condition lock')
    if enter.count('self._available') != 2:
        errors.append('FIFO admission may decrement capacity only once and cannot release active work')
    require('_FifoSlots.__exit__', ['with self._cond:', 'self._available += 1', 'self._cond.notify_all()'],
            'slot release must remain at actual context exit')
    sdk = ast.unparse(ast.parse(sdk_source))
    for fragment in ('http_client=client, max_retries=0, timeout=timeout',
                     'except APIStatusError as exc:\n        return exc.response',
                     'if exc.__cause__ is not None:\n            raise exc.__cause__'):
        if fragment not in sdk:
            errors.append('owned SDK client must disable hidden model retries and preserve response/transport observations')
            break
    return errors
