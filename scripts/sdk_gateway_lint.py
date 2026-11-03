import ast


GATEWAY = 'kb_platform/rag_testing/sdk_gateway.py'
CAPTURE = 'kb_platform/rag_testing/provider_capture.py'
MAIN = 'kb_platform/rag_testing/pageindex_full_index.py'
QUERY = 'kb_platform/rag_testing/pageindex_full_query.py'
ADMISSION = 'kb_platform/provider_admission.py'


def _shape(node):
    if isinstance(node, ast.AST):
        return type(node).__name__, tuple((name, _shape(value)) for name, value in ast.iter_fields(node)
                                         if name != 'ctx')
    if isinstance(node, list):
        return tuple(_shape(item) for item in node)
    return node


def _same(node, expression):
    return _shape(node) == _shape(ast.parse(expression, mode='eval').body)


def _assign(node, target, expression=None):
    return (isinstance(node, ast.Assign) and len(node.targets) == 1 and _same(node.targets[0], target)
            and (expression is None or _same(node.value, expression)))


def _call(node, expression):
    return isinstance(node, ast.Call) and _same(node.func, expression)


def _expression_call(node, expression):
    return isinstance(node, ast.Expr) and _call(node.value, expression)


def _with(node, expression):
    return (isinstance(node, ast.With) and len(node.items) == 1
            and _same(node.items[0].context_expr, expression))


def _raises(node):
    return bool(node.body) and isinstance(node.body[-1], ast.Raise) and not node.orelse


def _terms(node, operation):
    return node.values if isinstance(node, ast.BoolOp) and isinstance(node.op, operation) else [node]


def _matches_terms(node, operation, expressions):
    return {_shape(item) for item in _terms(node, operation)} == {
        _shape(ast.parse(expression, mode='eval').body) for expression in expressions}


def _method(group, name):
    found = [node for node in group.body if isinstance(node, ast.FunctionDef) and node.name == name]
    return found[0] if len(found) == 1 else None


def _single(nodes, predicate):
    found = [node for node in nodes if predicate(node)]
    return found[0] if len(found) == 1 else None


def _request_policy(handler):
    errors = []
    lock = _single(handler.body, lambda node: _with(node, 'gateway.lock'))
    if lock is None:
        return ['handler must serialize admission, dispatch, capture and failure under its owner lock']
    snapshots = [node for node in ast.walk(handler) if _assign(node, 'context', 'deepcopy(gateway._context)')]
    reads = [node for node in ast.walk(handler) if isinstance(node, ast.Attribute)
             and (_same(node, 'gateway.context') or _same(node, 'gateway._context'))]
    if len(snapshots) != 1 or snapshots[0] not in lock.body or reads != [snapshots[0].value.args[0]]:
        errors.append('request context must be one owned deep snapshot with no mutable rereads')
    attempt = _single(lock.body, lambda node: isinstance(node, ast.Try))
    if attempt is None:
        return errors + ['request failure must remain inside the same admission lock']
    failed = _single(attempt.handlers, lambda node: node.type is not None and _same(node.type, 'Exception'))
    if failed is None:
        errors.append('request failure must reset HTTP status and close the admission gate')
    else:
        blocked = _single(failed.body, lambda node: _assign(node, 'gateway.blocked', 'True'))
        status = _single(failed.body, lambda node: _assign(node, 'status'))
        body = _single(failed.body, lambda node: _assign(node, 'body'))
        valid_status = (status is not None and isinstance(status.value, ast.IfExp)
                        and _same(status.value.test, 'valid_request')
                        and all(isinstance(value, ast.Constant) and type(value.value) is int
                                and 400 <= value.value < 600
                                for value in (status.value.body, status.value.orelse)))
        if (blocked is None or body is None or not valid_status
                or failed.body.index(status) > failed.body.index(body)):
            errors.append('request failure must reset HTTP status and close the admission gate')
    gate = _single(attempt.body, lambda node: isinstance(node, ast.If) and _matches_terms(
        node.test, ast.Or, ['gateway.blocked', 'gateway.closing']) and _raises(node))
    duplicate = _single(attempt.body, lambda node: isinstance(node, ast.If) and _matches_terms(
        node.test, ast.And, ['key in gateway.seen', 'not cached']) and _raises(node))
    remembered = _single(attempt.body, lambda node: _expression_call(node, 'gateway.seen.add'))
    cached = _single(attempt.body, lambda node: _assign(node, 'cached'))
    selection = _single(attempt.body, lambda node: isinstance(node, ast.If) and _same(node.test, 'cached'))
    if (None in (gate, duplicate, remembered, cached, selection)
            or not (attempt.body.index(gate) < attempt.body.index(cached) < attempt.body.index(duplicate)
                    < attempt.body.index(remembered) < attempt.body.index(selection))
            or not _same(remembered.value, 'gateway.seen.add(key)')):
        errors.append('blocked/closing and duplicate guards must precede the one remembered dispatch')
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _same(node.func.value, 'gateway.seen'):
            if node is not (remembered.value if remembered is not None else None):
                errors.append('seen identities must never be erased or rewritten during a request')
    terminal_gate = _single(attempt.body, lambda node: isinstance(node, ast.If)
                           and _same(node.test, 'status >= 400')
                           and len(node.body) == 1 and _assign(node.body[0], 'gateway.blocked', 'True'))
    if terminal_gate is None or (selection is not None and attempt.body.index(terminal_gate) < attempt.body.index(selection)):
        errors.append('terminal HTTP errors must close the gate before another request is admitted')
    posts = [node for node in ast.walk(handler) if _call(node, 'gateway.client.post')]
    if len(posts) != 1 or selection is None or posts[0] not in list(ast.walk(selection)):
        errors.append('handler must own exactly one physical dispatch outside the cache-hit branch')
    if selection is not None:
        if any(_call(node, 'gateway.client.post') for item in selection.body for node in ast.walk(item)):
            errors.append('cache reuse must not send another physical request')
        errors.extend(_binding_policy(selection.orelse))
    return errors


def _binding_policy(statements):
    errors = []
    physical = _single(statements, lambda node: _assign(node, 'physical_context', 'deepcopy(context)'))
    dispatch = _single(statements, lambda node: _with(node, 'request_context(physical_context)'))
    binding = _single(statements, lambda node: isinstance(node, ast.If)
                      and _matches_terms(node.test, ast.And, ['status == 200', 'stage_key']))
    if (physical is None or dispatch is None or binding is None
            or not statements.index(physical) < statements.index(dispatch) < statements.index(binding)):
        return ['physical context and exact successful-capture binding must surround the dispatch']
    identity = _single(binding.body, lambda node: _assign(node, 'physical_id',
                                                         'response.extensions["_kb_physical_request_id"]'))
    record = _single(binding.body, lambda node: _assign(node, 'record',
        'json.loads((output / "physical-requests" / (physical_id + ".json")).read_text("utf-8"))'))
    checks = ['record["physical_request_id"] != physical_id', 'record["payload_sha256"] != digest(payload)',
              'record["payload"] != payload', 'record["context"] != physical_context',
              'record["status"] != "COMPLETE"', 'capture_unresolved(record)',
              'record["http_status"] != 200', 'record["response"] != body',
              'record["provider_scope"] != admission.scope_hash']
    guard = _single(binding.body, lambda node: isinstance(node, ast.If)
                    and _matches_terms(node.test, ast.Or, checks) and _raises(node))
    publish = _single(binding.body, lambda node: _expression_call(node, 'gateway.recovery_cache.put'))
    if (None in (identity, record, guard, publish)
            or not (binding.body.index(identity) < binding.body.index(record) < binding.body.index(guard)
                    < binding.body.index(publish))
            or not _same(publish.value,
                         'gateway.recovery_cache.put(stage_key, dict(response=body, physical_id=physical_id))')):
        errors.append('cache binding must verify the exact new physical identity, context, payload and settled response')
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and node.func.attr in {'glob', 'rglob', 'iterdir'} for node in ast.walk(binding)):
        errors.append('new capture binding must not scan by payload or document')
    return errors


def _context_policy(group):
    methods = [node for node in group.body if isinstance(node, ast.FunctionDef) and node.name == 'context']
    getters = [node for node in methods if any(_same(decorator, 'property') for decorator in node.decorator_list)]
    setters = [node for node in methods if any(_same(decorator, 'context.setter') for decorator in node.decorator_list)]
    if len(getters) != 1 or len(setters) != 1:
        return ['context ownership requires locked deep-copy getter and setter']
    getter, setter = getters[0], setters[0]
    get_lock = _single(getter.body, lambda node: _with(node, 'self.lock'))
    set_lock = _single(setter.body, lambda node: _with(node, 'self.lock'))
    if (get_lock is None or set_lock is None or len(get_lock.body) != 1
            or not isinstance(get_lock.body[0], ast.Return) or not _same(get_lock.body[0].value, 'deepcopy(self._context)')
            or _single(set_lock.body, lambda node: _assign(node, 'self._context', 'deepcopy(value)')) is None):
        return ['context ownership requires locked deep-copy getter and setter']
    guard = _single(set_lock.body, lambda node: isinstance(node, ast.If)
                    and _same(node.test, 'self.closing') and _raises(node))
    publish = _single(set_lock.body, lambda node: _assign(node, 'self._context', 'deepcopy(value)'))
    if guard is None or set_lock.body.index(guard) > set_lock.body.index(publish):
        return ['context replacement must reject a closing owner']
    key = _method(group, 'stage_key')
    calls = [] if key is None else [node for node in ast.walk(key) if _call(node, 'StageKey')]
    upstream = [] if len(calls) != 1 else [item.value for item in calls[0].keywords if item.arg == 'upstream']
    expected = '(digest(payload), digest({k: v for k, v in context.items() if k not in {"retry_of", "retry_reason"}}))'
    if len(upstream) != 1 or not _same(upstream[0], expected):
        return ['cache identity must include the complete logical context, excluding only retry lineage']
    return []


def _close_policy(initializer, close):
    errors = []
    daemon = _single(initializer.body, lambda node: _assign(node, 'self.server.daemon_threads', 'False'))
    blocking = _single(initializer.body, lambda node: _assign(node, 'self.server.block_on_close', 'True'))
    start = _single(initializer.body, lambda node: _expression_call(node, 'self.thread.start'))
    if None in (daemon, blocking, start) or not (initializer.body.index(daemon) < initializer.body.index(start)
                                               and initializer.body.index(blocking) < initializer.body.index(start)):
        errors.append('owned server handlers must be non-daemon and joined by server_close')
    serialized = _single(close.body, lambda node: _with(node, 'self.close_lock'))
    if serialized is None:
        return errors + ['close must serialize and drain handlers before releasing client/cache ownership']
    lock = _single(serialized.body, lambda node: _with(node, 'self.lock'))
    calls = [_single(serialized.body, lambda node: _expression_call(node, method)) for method in
             ('self.server.shutdown', 'self.server.server_close', 'self.thread.join', 'self.client.close')]
    cache = _single(serialized.body, lambda node: isinstance(node, ast.If)
                    and _same(node.test, 'self.recovery_cache is not None') and len(node.body) == 1
                    and _expression_call(node.body[0], 'self.recovery_cache.close'))
    closed = _single(serialized.body, lambda node: _assign(node, 'self.closed', 'True'))
    ordered = [lock, *calls, cache, closed]
    if (None in ordered or lock is None or len(lock.body) != 1
            or not _assign(lock.body[0], 'self.closing', 'True')
            or [serialized.body.index(node) for node in ordered] != sorted(serialized.body.index(node) for node in ordered)):
        errors.append('close must serialize and drain handlers before releasing client/cache ownership')
    return errors


def _capture_policy(tree):
    group = _single(tree.body, lambda node: isinstance(node, ast.ClassDef) and node.name == 'MeteredTransport')
    method = None if group is None else _method(group, '_handle_admitted')
    if method is None:
        return ['capture transport must publish the exact physical identity after settlement']
    attempts = [node for node in method.body if isinstance(node, ast.Try) and any(
        _assign(item, 'response.extensions["_kb_physical_request_id"]', 'identity') for item in node.body)]
    if len(attempts) != 1:
        return ['capture transport must publish the exact physical identity after settlement']
    body = attempts[0].body
    identity = _single(body, lambda node: _assign(node, 'response.extensions["_kb_physical_request_id"]', 'identity'))
    settle = _single(body, lambda node: _expression_call(node, 'settle'))
    returned = _single(body, lambda node: isinstance(node, ast.Return) and _same(node.value, 'response'))
    if None in (settle, returned) or not body.index(settle) < body.index(identity) < body.index(returned):
        return ['capture transport must publish the exact physical identity after settlement']
    return []


def _recovery_policy(initializer):
    recovery = _single(initializer.body, lambda node: isinstance(node, ast.If) and _same(node.test, 'recovery'))
    if recovery is None:
        return ['recovery must validate every capture before cache publication or retry selection']
    validated = _single(recovery.body, lambda node: isinstance(node, ast.For)
                        and _same(node.target, 'p') and _same(node.iter, 'paths'))
    grouped = _single(recovery.body, lambda node: isinstance(node, ast.For)
                      and _same(node.iter, 'groups.items()'))
    if validated is None or grouped is None or recovery.body.index(validated) >= recovery.body.index(grouped):
        return ['recovery must validate every capture before cache publication or retry selection']
    scope = _single(recovery.body, lambda node: isinstance(node, ast.If) and _same(node.test,
        'any(old.get("provider_scope") != admission.scope_hash for old in self.recovered_records)') and _raises(node))
    admission = _single(recovery.body, lambda node: _assign(node, 'admission',
        'ProviderAdmission(self.provider, self.provider.cache_dir / "provider-admission")'))
    if (scope is None or admission is None or not (recovery.body.index(validated)
            < recovery.body.index(admission) < recovery.body.index(scope) < recovery.body.index(grouped))):
        return ['recovery must verify the semantic provider scope before publishing cache or retry lineage']
    unresolved = _single(validated.body, lambda node: isinstance(node, ast.If)
                         and _same(node.test, 'capture_unresolved(old)') and _raises(node))
    grouped_record = _single(validated.body, lambda node: _assign(node,
        'groups.setdefault(digest(key), (key, {}))[1][identity]', 'old'))
    if (unresolved is None or grouped_record is None
            or validated.body.index(unresolved) >= validated.body.index(grouped_record)):
        return ['recovery must reject unresolved provider/settlement facts before grouping records']
    completed = _single(grouped.body, lambda node: isinstance(node, ast.If) and _same(node.test, 'completed'))
    if completed is None:
        return ['recovery requires separate completed-cache and terminal-error retry branches']
    unique = _single(completed.body, lambda node: isinstance(node, ast.If) and _matches_terms(node.test, ast.Or,
        ['len(completed) != 1', 'completed[0].get("http_status") != 200']) and _raises(node))
    published = _single(completed.body, lambda node: _expression_call(node, 'self.recovery_cache.put'))
    if unique is None or published is None or completed.body.index(unique) >= completed.body.index(published):
        return ['recovery cache requires one replayable completed physical result']
    unique_leaf = _single(completed.orelse, lambda node: isinstance(node, ast.If)
                         and _same(node.test, 'len(leaves) != 1') and _raises(node))
    lineage = _single(completed.orelse, lambda node: isinstance(node, ast.If) and _matches_terms(node.test, ast.Or,
        ['linked != set(group)', 'current in linked']) and _raises(node))
    selected = _single(completed.orelse, lambda node: _assign(node, 'self.retry_of[key_hash]', 'latest'))
    walk = _single(completed.orelse, lambda node: isinstance(node, ast.While) and _matches_terms(node.test,
        ast.And, ['current in group', 'current not in linked']))
    if (None in (unique_leaf, walk, lineage, selected)
            or not (completed.orelse.index(unique_leaf) < completed.orelse.index(walk)
                    < completed.orelse.index(lineage) < completed.orelse.index(selected))):
        return ['retry selection must validate a unique complete acyclic lineage before choosing its leaf']
    return []


def _main_policy(tree):
    main = _single(tree.body, lambda node: isinstance(node, ast.FunctionDef) and node.name == 'main')
    if main is None:
        return ['main must retain the gateway lifetime owner']
    attempts = [node for node in main.body if isinstance(node, ast.Try) and any(
        _call(item, 'PageIndexClient') for statement in node.body for item in ast.walk(statement))]
    constructors = [node for node in ast.walk(main) if _call(node, 'PageIndexClient')]
    if len(attempts) != 1 or len(constructors) != 1:
        return ['PageIndexClient construction must be protected by gateway-close finally']
    cleanup = attempts[0].finalbody
    closed = _single(cleanup, lambda node: _expression_call(node, 'gateway.close'))
    summary = _single(cleanup, lambda node: isinstance(node, ast.Expr) and _same(node.value,
        'write_json(args.output / "budget-after.json", ledger.cumulative_summary())'))
    if closed is None or summary is None or cleanup.index(closed) >= cleanup.index(summary):
        return ['main must drain the owned gateway before reading and publishing final budget']
    return []


def _provider_scope_policy(tree):
    group = _single(tree.body, lambda node: isinstance(node, ast.ClassDef) and node.name == 'ProviderAdmission')
    initializer = None if group is None else _method(group, '__init__')
    if initializer is None:
        return ['existing provider/model/endpoint scope authority must remain available']
    identity = _single(initializer.body, lambda node: _assign(node, 'identity',
        "{'provider_id': provider.provider_id, 'model': provider.model, "
        "'dimension': getattr(provider, 'dimension', None), "
        "'endpoint_sha256': digest(provider.manifest.get('endpoint', provider.base_url))}"))
    published = _single(initializer.body, lambda node: _assign(node, 'self.scope_hash', 'digest(identity)'))
    if identity is None or published is None or initializer.body.index(identity) >= initializer.body.index(published):
        return ['existing provider/model/endpoint scope must not become a path hash or constant']
    return []


def violations(root):
    try:
        tree = ast.parse((root / GATEWAY).read_text('utf-8'))
        capture = ast.parse((root / CAPTURE).read_text('utf-8'))
        group = _single(tree.body, lambda node: isinstance(node, ast.ClassDef) and node.name == 'SDKGateway')
        initializer = None if group is None else _method(group, '__init__')
        close = None if group is None else _method(group, 'close')
        handlers = [] if initializer is None else [node for node in initializer.body
                    if isinstance(node, ast.ClassDef) and node.name == 'Handler']
        handler = _method(handlers[0], 'do_POST') if len(handlers) == 1 else None
        if None in (group, initializer, close, handler):
            return ['sdk gateway: explicit gateway, handler and close owners required']
        errors = (_request_policy(handler) + _context_policy(group)
                  + _close_policy(initializer, close) + _capture_policy(capture) + _recovery_policy(initializer))
        if (root / ADMISSION).exists():
            errors.extend(_provider_scope_policy(ast.parse((root / ADMISSION).read_text('utf-8'))))
        for path in (MAIN, QUERY):
            if (root / path).exists():
                errors.extend(_main_policy(ast.parse((root / path).read_text('utf-8'))))
        return ['sdk gateway: ' + error for error in errors]
    except (OSError, SyntaxError, AttributeError, TypeError, ValueError) as exc:
        return ['sdk gateway: owner unreadable or malformed: ' + type(exc).__name__]
