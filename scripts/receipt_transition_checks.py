import ast


def _expression(source):
    return ast.dump(ast.parse(source, mode='eval').body, include_attributes=False)


def _same(node, source):
    return ast.dump(node, include_attributes=False) == _expression(source)


def _nodes(node, ancestors=()):
    yield node, ancestors
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _nodes(child, (*ancestors, node))


def receipt_transition_violations(source):
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'RequestReceipts']
    if len(classes) != 1:
        return ['file receipt transition owner is missing or ambiguous']
    errors = []
    for name in ('begin', 'finish', 'reconcile'):
        methods = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(methods) != 1:
            errors.append('file receipt transition missing: ' + name)
            continue
        method = methods[0]
        expected = "sqlite_guard(self._pending(logical_id).with_suffix('.guard.sqlite3'), nowait=False)"
        guards = [node for node, _ in _nodes(method) if isinstance(node, ast.With)
                  and len(node.items) == 1 and _same(node.items[0].context_expr, expected)]
        if len(guards) != 1:
            errors.append(name + ': matching logical SQLite guard required')
            continue
        guard = guards[0]
        for node, ancestors in _nodes(method):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {'_write', '_write_path', 'read_text', 'open', 'unlink'}
                    and guard not in ancestors):
                errors.append(name + ': receipt read/write/removal outside logical guard')
        if name == 'begin':
            continue
        checks = []
        for statement in guard.body:
            if not isinstance(statement, ast.If) or not isinstance(statement.test, ast.BoolOp):
                continue
            if not isinstance(statement.test.op, ast.Or):
                continue
            branches = statement.test.values
            matching = any(_same(value, "active['physical_request_id'] != physical_id") for value in branches)
            missing = any(_same(value, 'active is None') for value in branches)
            code = 'INVALID_PHYSICAL_REQUEST_TRANSITION' if name == 'finish' else 'INVALID_REQUEST_RECONCILIATION'
            rejection = any(isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                            and _same(node.exc, 'RuntimePolicyError(' + repr(code) + ')')
                            for node in statement.body)
            if matching and missing and rejection:
                checks.append(statement)
        publications = [node for node, _ in _nodes(guard) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute) and node.func.attr in {'_write', '_write_path', 'unlink'}]
        if len(checks) != 1 or any(node.lineno < checks[0].lineno for node in publications):
            errors.append(name + ': active physical identity rejection must precede publication')
    return errors


def sqlite_guard_violations(source):
    tree = ast.parse(source)
    methods = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'sqlite_guard']
    if len(methods) != 1:
        return ['SQLite guard primitive missing or ambiguous']
    method = methods[0]
    tries = [node for node in method.body if isinstance(node, ast.Try)]
    if len(tries) != 1:
        return ['SQLite guard must close its per-operation connection in finally']
    outer = tries[0]
    closed = any(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                 and _same(node.value, 'connection.close()') for node in outer.finalbody)
    acquired = [node for node in ast.walk(outer) if isinstance(node, ast.Call)
                and _same(node, "connection.execute('BEGIN IMMEDIATE')")]
    yielded = [node for node in outer.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Yield)]
    if not closed or len(acquired) != 1 or len(yielded) != 1 or acquired[0].lineno > yielded[0].lineno:
        return ['SQLite guard must acquire BEGIN IMMEDIATE before yielding and close in finally']
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and node.func.attr in {'unlink', 'remove', 'replace', 'rename'} for node in ast.walk(method)):
        return ['SQLite guard identity must not be removed or replaced']
    return []
