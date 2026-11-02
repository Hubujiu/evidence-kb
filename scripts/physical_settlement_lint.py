import ast
from pathlib import Path


OWNERS = (
    ('kb_platform/rag_testing/provider_capture.py', 'MeteredTransport', '_handle_admitted', 'BaseException'),
    ('kb_platform/google_semantic_provider.py', 'GoogleSemanticProvider', '_observed', 'BaseException'),
)


CAPTURE_CONSUMERS = (
    ('kb_platform/rag_testing/delivery_http.py', 'require_no_pending', 'pending', 'record'),
    ('kb_platform/rag_testing/delivery_http.py', '_physical_summary', 'pending', 'record'),
    ('kb_platform/rag_testing/delivery_http.py', 'capture_final', 'reject_any', 'records'),
    ('kb_platform/rag_testing/sdk_gateway.py', 'SDKGateway.__init__', 'reject', 'old'),
    ('kb_platform/rag_testing/reader_diagnostic.py', 'FullCorpusReader.run', 'reject', 'bound[0]'),
    ('kb_platform/rag_testing/pageindex_probe.py', 'PageIndexAdapter.run', 'reject_any', 'bound'),
    ('kb_platform/rag_testing/pageindex_full_query.py', 'PDFAdapter.run', 'reject_any', 'bound'),
)


def _shape(node):
    if isinstance(node, ast.AST):
        return type(node).__name__, tuple((field, _shape(value)) for field, value in ast.iter_fields(node)
                                         if field != 'ctx')
    if isinstance(node, list):
        return tuple(_shape(value) for value in node)
    return node


def _same(node, source):
    return _shape(node) == _shape(ast.parse(source, mode='eval').body)


def _walk(nodes):
    for node in nodes:
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _walk(ast.iter_child_nodes(node))


def _calls(nodes, predicate):
    return [node for node in _walk(nodes) if isinstance(node, ast.Call) and predicate(node.func)]


def _bare_raise(node):
    return isinstance(node, ast.Raise) and node.exc is None and node.cause is None


def _marker_update(node):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and _same(node.value.func, 'record.update')
            and any(keyword.arg == 'ledger_settlement' and _same(keyword.value, "'UNCONFIRMED'")
                    for keyword in node.value.keywords))


def _confirmed_assignment(node):
    return (isinstance(node, ast.Assign) and len(node.targets) == 1
            and _same(node.targets[0], "record['ledger_settlement']")
            and _same(node.value, "'COMMITTED'"))


def _failed_assignment(node, value):
    return (isinstance(node, ast.Assign) and len(node.targets) == 1
            and _same(node.targets[0], 'failed') and _same(node.value, str(value)))


def _guard(node):
    return (isinstance(node, ast.If) and _same(node.test, "'ledger_settlement' in record")
            and len(node.body) == 1 and _bare_raise(node.body[0]) and not node.orelse)


def _owner_violations(function, exception_type):
    errors = []
    helpers = [node for node in function.body if isinstance(node, ast.FunctionDef) and node.name == 'settle']
    if len(helpers) != 1:
        return ['single local settlement helper required']
    helper = helpers[0]
    def physical(node):
        return _same(node, 'self.ledger.finish')

    def settle(node):
        return isinstance(node, ast.Name) and node.id == 'settle'

    physical_calls = _calls(helper.body, physical)
    all_physical = [node for node in ast.walk(function) if isinstance(node, ast.Call) and physical(node.func)]
    if len(physical_calls) != 1 or len(all_physical) != 1:
        errors.append('physical finish must occur once and only inside settlement helper')
    valid_helper = (len(helper.body) == 3 and _marker_update(helper.body[0])
                    and isinstance(helper.body[1], ast.Try) and _confirmed_assignment(helper.body[2]))
    if valid_helper:
        attempt = helper.body[1]
        valid_helper = (len(attempt.body) == 1 and isinstance(attempt.body[0], ast.Expr)
                        and isinstance(attempt.body[0].value, ast.Call)
                        and physical(attempt.body[0].value.func) and len(attempt.handlers) == 1
                        and not attempt.orelse and not attempt.finalbody)
        if valid_helper:
            handler = attempt.handlers[0]
            valid_helper = (handler.type is not None and _same(handler.type, 'BaseException')
                            and bool(handler.body) and _bare_raise(handler.body[-1])
                            and all(not isinstance(node, (ast.Return, ast.Break, ast.Continue))
                                    and (not isinstance(node, ast.Raise) or _bare_raise(node))
                                    for node in _walk(handler.body)))
    if not valid_helper:
        errors.append('settlement must mark before finish, preserve original exception, confirm only after success')
    attempts = [node for node in _walk(function.body) if isinstance(node, ast.Try)
                and _calls(node.body, settle)]
    if len(attempts) != 1 or len(_calls(function.body, settle)) != 2:
        errors.append('one success settlement and one guarded failure settlement required')
    else:
        attempt = attempts[0]
        valid_attempt = (len(_calls(attempt.body, settle)) == 1 and len(attempt.handlers) == 1
                         and not _calls(attempt.finalbody, settle) and not _calls(attempt.orelse, settle))
        if valid_attempt:
            handler = attempt.handlers[0]
            valid_attempt = (handler.type is not None and _same(handler.type, exception_type)
                             and len(handler.body) >= 3 and _failed_assignment(handler.body[0], True)
                             and _guard(handler.body[1])
                             and _bare_raise(handler.body[-1]) and len(_calls(handler.body, settle)) == 1)
        preceding = function.body[:function.body.index(attempt)] if attempt in function.body else []
        valid_attempt = valid_attempt and any(_failed_assignment(node, False) for node in preceding)
        if not valid_attempt:
            errors.append('outer failure handler must immediately rethrow after settlement has begun')
    for node in ast.walk(function):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)) and _calls((node,), settle):
            errors.append('settlement must not be repeated by a loop')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _same(node.func.value, 'record'):
            if node.func.attr == 'clear' or node.func.attr in {'pop', '__delitem__'} and node.args and _same(
                    node.args[0], "'ledger_settlement'"):
                errors.append('settlement marker must not be erased')
            if (node.func.attr == 'update' and any(k.arg == 'ledger_settlement' for k in node.keywords)
                    and not (isinstance(helper.body[0], ast.Expr) and node is helper.body[0].value)):
                errors.append('settlement marker is owned by settlement helper')
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if (_same(target, "record['ledger_settlement']") and node is not helper.body[-1]) or (
                        isinstance(target, ast.Name) and target.id == 'record' and node.lineno > helper.lineno):
                    errors.append('settlement marker must not be replaced outside its helper')
    return errors


def _named(body, symbol):
    nodes = body
    selected = None
    for name in symbol.split('.'):
        found = [node for node in nodes if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name]
        if len(found) != 1:
            return None
        selected = found[0]
        nodes = selected.body
    return selected


def _capture_boundary_violations(function):
    errors = []
    def dispatch(node):
        return _same(node, 'self.inner.handle_request')

    attempts = [node for node in function.body if isinstance(node, ast.Try) and _calls(node.body, dispatch)]
    preflights = [node for node in function.body if isinstance(node, ast.Try) and len(node.handlers) == 1
                 and node.handlers[0].name == 'exc' and len(node.handlers[0].body) == 2
                 and _shape(node.handlers[0].body) == _shape(ast.parse('exc.model_sent = False\nraise').body)]
    valid = len(attempts) == len(preflights) == 1 and len(_calls(function.body, dispatch)) == 1
    if valid:
        preflight, attempt = preflights[0], attempts[0]
        valid = (function.body.index(preflight) < function.body.index(attempt)
                 and preflight.handlers[0].type is not None
                 and _same(preflight.handlers[0].type, 'BaseException')
                 and not preflight.orelse and not preflight.finalbody
                 and not _calls(preflight.body, lambda node: dispatch(node) or
                     _same(node, 'settle') or _same(node, 'preserve_observed')))
        markers = [node for node in ast.walk(function) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Attribute) and target.attr == 'model_sent'
                           for target in node.targets)]
        valid = valid and markers == [preflight.handlers[0].body[0]]
    if not valid:
        errors.append('known-zero-send marker must remain confined to the separate pre-dispatch block')
    expected = ast.parse("def preserve_observed(error):\n"
        "    if 'http_status' in record:\n"
        "        body = record.get('response')\n"
        "        usage = body.get('usage') if model and isinstance(body, dict) else None\n"
        "        preserve_result_failure(error, usage, physical_request_id=identity)\n").body[0]
    helpers = [node for node in function.body if isinstance(node, ast.FunctionDef)
               and node.name == 'preserve_observed']
    canonical = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                 and _same(node.func, 'preserve_result_failure')]
    if len(helpers) != 1 or _shape(helpers[0]) != _shape(expected) or len(canonical) != 1:
        errors.append('observed-result boundary must preserve only raw provider usage after an observed response')
    helper = _named(function.body, 'settle')
    if helper is not None and len(helper.body) > 1 and isinstance(helper.body[1], ast.Try):
        handlers = helper.body[1].handlers
        if (len(handlers) != 1 or not handlers[0].body or
                _shape(handlers[0].body[0]) != _shape(ast.parse('preserve_observed(error)').body[0])):
            errors.append('settlement failure must preserve observed result before bare rethrow')
    else:
        errors.append('observed-result settlement handler missing')
    if len(attempts) == 1:
        attempt = attempts[0]
        handlers = attempt.handlers
        if (len(handlers) != 1 or len(handlers[0].body) < 3 or _shape(handlers[0].body[2]) !=
                _shape(ast.parse('preserve_observed(exc)').body[0])):
            errors.append('post-response and capture-publication failures must retain observed result facts')
        final = ast.parse("record['elapsed_seconds'] = time.perf_counter() - started\n"
            "failure = sys.exception() if failed else None\n"
            "try:\n    save()\n"
            "except Exception as capture_error:\n"
            "    if failure is None:\n        preserve_observed(capture_error)\n        raise\n"
            "    failure.add_note('Physical capture persistence failed: ' + type(capture_error).__name__)\n").body
        if _shape(attempt.finalbody) != _shape(final):
            errors.append('capture publication must retain observed result or note, never replace the original error')
    return errors


def _boolean_contains(node, expression, operator):
    return (_same(node, expression) or isinstance(node, ast.BoolOp) and isinstance(node.op, operator)
            and any(_boolean_contains(value, expression, operator) for value in node.values))


def _capture_violations(root):
    errors = []
    path = 'kb_platform/rag_testing/provider_capture.py'
    try:
        tree = ast.parse((Path(root) / path).read_text('utf-8'))
        helper = _named(tree.body, 'capture_unresolved')
        expression = ("record.get('status') not in ('COMPLETE', 'ERROR') or "
                      "'ledger_settlement' in record and record['ledger_settlement'] != 'COMMITTED'")
        if (helper is None or len(helper.body) != 1 or not isinstance(helper.body[0], ast.Return)
                or not _same(helper.body[0].value, expression)):
            errors.append('capture authority: terminal provider status must not hide unconfirmed settlement')
    except (OSError, SyntaxError, UnicodeError):
        errors.append('capture authority: unresolved predicate unreadable')
    for path, symbol, mode, argument in CAPTURE_CONSUMERS:
        try:
            tree = ast.parse((Path(root) / path).read_text('utf-8'))
            owner = _named(tree.body, symbol)
            predicate = 'capture_unresolved(' + argument + ')'
            imported = any(isinstance(node, ast.ImportFrom) and node.module == 'provider_capture'
                and any(alias.name == 'capture_unresolved' and alias.asname is None for alias in node.names)
                for node in tree.body)
            valid = False
            if owner is not None and imported:
                for node in ast.walk(owner):
                    if mode == 'pending' and isinstance(node, ast.comprehension):
                        valid = valid or any(_boolean_contains(condition, predicate, ast.And) for condition in node.ifs)
                    elif (isinstance(node, ast.If) and len(node.body) == 1 and isinstance(node.body[0], ast.Raise)
                          and (mode == 'reject_any' and _same(node.test,
                               'any(capture_unresolved(record) for record in ' + argument + ')')
                               or mode == 'reject' and _boolean_contains(node.test, predicate, ast.Or))):
                        valid = True
            if not valid:
                errors.append(f'capture authority: {path}:{symbol}: workflow must gate on unresolved settlement')
        except (OSError, SyntaxError, UnicodeError):
            errors.append(f'capture authority: {path}:{symbol}: consumer unreadable')
    return errors


def settlement_violations(root: Path) -> list[str]:
    errors = []
    for path, class_name, method_name, exception_type in OWNERS:
        try:
            tree = ast.parse((Path(root) / path).read_text('utf-8'))
            classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
            methods = [node for group in classes for node in group.body
                       if isinstance(node, ast.FunctionDef) and node.name == method_name]
            found = (_owner_violations(methods[0], exception_type) if len(methods) == 1
                     else ['settlement owner missing or duplicated'])
            if class_name == 'MeteredTransport' and len(methods) == 1:
                found.extend(_capture_boundary_violations(methods[0]))
        except (OSError, SyntaxError, UnicodeError) as error:
            found = ['settlement owner unreadable: ' + type(error).__name__]
        errors.extend(f'physical settlement: {path}:{class_name}.{method_name}: {message}' for message in found)
    errors.extend(_capture_violations(root))
    return errors
