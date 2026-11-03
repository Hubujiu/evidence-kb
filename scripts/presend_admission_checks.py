import ast


def _same(node, expression):
    return ast.dump(node, include_attributes=False).replace('ctx=Store()', 'ctx=Load()') == ast.dump(
        ast.parse(expression, mode='eval').body, include_attributes=False)


def presend_admission_violations(source):
    tree = ast.parse(source)
    methods = [method for node in tree.body if isinstance(node, ast.ClassDef) and node.name == '_Transport'
               for method in node.body if isinstance(method, ast.FunctionDef) and method.name == '_send_attempt']
    if len(methods) != 1:
        return ['provider pre-send owner missing or ambiguous']
    method = methods[0]
    guards = [node for node in method.body if isinstance(node, ast.With) and len(node.items) == 1
              and _same(node.items[0].context_expr, 'self._stats_lock')]
    if len(guards) != 1:
        return ['provider receipt admission and issued accounting must share the stats lock']
    guard = guards[0]
    begun = [node for node in ast.walk(guard) if isinstance(node, ast.Call)
             and _same(node, 'self._receipts.begin(logical_id, key, metadata)')]
    checks = [node for node in guard.body if isinstance(node, ast.Try) and len(node.body) == 1
              and isinstance(node.body[0], ast.Assign) and len(node.body[0].targets) == 1
              and _same(node.body[0].targets[0], 'remaining') and _same(node.body[0].value, 'remaining_time()')]
    issued = [node for node in ast.walk(method) if isinstance(node, ast.AugAssign)
              and _same(node.target, 'self._issued_requests')]
    physical = [node for node in method.body if isinstance(node, ast.Assign)
                and any(_same(target, "metadata['physical_request_id']") for target in node.targets)]
    send = [node for node in ast.walk(method) if isinstance(node, ast.Call)
            and _same(node, 'self._request_response(path, payload, headers, timeout)')]
    errors = []
    if (len(begun) != 1 or len(checks) != 1 or len(issued) != 1 or len(physical) != 1 or len(send) != 1
            or not (begun[0].lineno < checks[0].lineno < issued[0].lineno < physical[0].lineno < send[0].lineno)
            or issued[0] not in guard.body):
        return ['fresh post-receipt admission must dominate issued accounting, physical identity and send']
    helper = [node for node in method.body if isinstance(node, ast.FunctionDef) and node.name == 'remaining_time']
    if len(helper) != 1:
        return ['fresh admission helper missing or ambiguous']
    required = ("execution.cancellation.is_set()", "deadline - time.monotonic()",
                "remaining <= 0", "RemoteModelError('PROVIDER_CANCELLED')",
                "RemoteModelError('PROVIDER_DEADLINE_EXCEEDED')")
    if any(not any(_same(node, expression) for node in ast.walk(helper[0])) for expression in required):
        errors.append('fresh admission must check current cancellation and monotonic deadline')
    timeout = [node for node in ast.walk(method) if isinstance(node, ast.Assign)
               and any(_same(target, 'timeout') for target in node.targets)]
    if len(timeout) != 1 or not _same(timeout[0].value,
            'min(self.timeout, remaining) if remaining is not None else self.timeout'):
        errors.append('dispatch timeout must use the refreshed remaining time')
    handlers = checks[0].handlers
    if (len(handlers) != 1 or handlers[0].name != 'preflight'
            or not _same(handlers[0].type, 'RemoteModelError')):
        return [*errors, 'fresh admission refusal must have a dedicated known-unsent handler']
    handler = handlers[0]
    facts = [node for node in handler.body if isinstance(node, ast.Assign)
             and any(_same(target, 'facts') for target in node.targets)]
    expected = "{'model_sent': False, 'preflight_code': preflight.code, 'admission_receipt_id': physical_id, 'known_zero_send': True}"
    if len(facts) != 1 or not _same(facts[0].value, expected):
        errors.append('known-unsent facts must preserve preflight and receipt identity without claiming a send')
    finalizations = [node for node in ast.walk(handler) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == 'finish']
    if len(finalizations) != 1 or not _same(finalizations[0],
            "self._receipts.finish(logical_id, physical_id, 'ERROR', **facts)"):
        errors.append('known-unsent receipt must be finalized exactly once as ERROR')
    if not isinstance(handler.body[-1], ast.Raise) or handler.body[-1].exc is not None:
        errors.append('known-unsent refusal must exit without reaching send')
    storage = [node for node in ast.walk(handler) if isinstance(node, ast.ExceptHandler)
               and node.name == 'storage_error']
    if (len(storage) != 1 or not isinstance(storage[0].body[-1], ast.Raise)
            or storage[0].body[-1].exc is not None
            or not any(isinstance(node, ast.Call) and _same(node, 'setattr(storage_error, name, value)')
                       for node in ast.walk(storage[0]))):
        errors.append('uncertain receipt publication must preserve the original storage exception and facts')
    return errors
