import ast


def _expression(node, source):
    return node is not None and ast.dump(node) == ast.dump(ast.parse(source, mode='eval').body)


def _statement(node, source):
    return node is not None and ast.dump(node) == ast.dump(ast.parse(source).body[0])


def _self_attribute(node):
    return node.attr if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self' else None


def provider_circuit_violations(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ['provider-circuit: invalid Python source']
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    if 'ProviderCircuit' not in classes or '_CircuitPermit' not in classes:
        return ['provider-circuit: circuit and identity permit classes are required']
    errors = []
    identity = False
    for decorator in classes['_CircuitPermit'].decorator_list:
        if isinstance(decorator, ast.Call) and _expression(decorator.func, 'dataclass'):
            keywords = {item.arg: item.value for item in decorator.keywords}
            identity = _expression(keywords.get('eq'), 'False') and _expression(keywords.get('frozen'), 'True')
    if not identity:
        errors.append('provider-circuit: permit equality must be immutable identity')
    methods = {node.name: node for node in classes['ProviderCircuit'].body if isinstance(node, ast.FunctionDef)}
    finish = methods.get('finish')
    locked = (finish is not None and len(finish.body) == 1 and isinstance(finish.body[0], ast.With)
              and len(finish.body[0].items) == 1 and finish.body[0].items[0].optional_vars is None
              and _expression(finish.body[0].items[0].context_expr, 'self._lock'))
    body = finish.body[0].body if locked else []
    guard = body[0] if body else None
    fenced = (isinstance(guard, ast.If) and not guard.orelse and len(guard.body) == 1
              and isinstance(guard.body[0], ast.Return) and guard.body[0].value is None
              and _expression(guard.test, 'permit not in self._active or permit.generation != self._generation'))
    if not locked or not fenced:
        errors.append('provider-circuit: finish must lock then reject inactive or stale permits before any effects')
    if len(body) < 2 or not _statement(body[1], 'self._active.remove(permit)'):
        errors.append('provider-circuit: finish must consume its permit immediately after the rejection fence')
    if len(body) < 3 or not _statement(body[2], 'probing = permit is self._probe'):
        errors.append('provider-circuit: finish must derive probe ownership from permit identity')
    releases = [node for node in ast.walk(finish) if _statement(
        node, 'if probing:\n    self._probe = None\n    self._advance()')] if finish else []
    if len(releases) != 1:
        errors.append('provider-circuit: probe release must advance generation under the ownership condition')
    owned_stores = {id(node) for release in releases for node in ast.walk(release)}
    writers = {'_opened_at': {'__init__', 'finish'}, '_failures': {'__init__', 'finish'},
               '_probe': {'__init__', 'acquire', 'finish'}, '_generation': {'__init__', '_advance'},
               '_active': {'__init__'}}
    active_calls = {'add': '_permit', 'clear': '_advance', 'remove': 'finish'}
    for name, method in methods.items():
        for node in ast.walk(method):
            attribute = _self_attribute(node)
            if attribute in writers and isinstance(node.ctx, (ast.Store, ast.Del)):
                if name not in writers[attribute]:
                    errors.append(f'provider-circuit: {name} cannot write lifecycle field {attribute}')
                if name == 'finish' and attribute == '_probe' and id(node) not in owned_stores:
                    errors.append('provider-circuit: finish cannot release a probe outside its owner condition')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if _self_attribute(node.func.value) == '_active' and active_calls.get(node.func.attr) != name:
                    errors.append(f'provider-circuit: {name} cannot mutate active permits using {node.func.attr}')
                if _self_attribute(node.func) == '_advance' and name not in {'acquire', 'finish'}:
                    errors.append(f'provider-circuit: {name} cannot advance a circuit generation')
    return errors
