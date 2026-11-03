import ast


def _same(node, source):
    return isinstance(node, ast.AST) and ast.dump(node, include_attributes=False) == ast.dump(
        ast.parse(source, mode='eval').body, include_attributes=False)


def semantic_manifest_violations(source):
    tree = ast.parse(source)
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SemanticPageService']
    methods = [node for owner in owners for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == 'current_manifest']
    if len(owners) != 1 or len(methods) != 1:
        return ['semantic manifest postflight owner is missing or ambiguous']
    method = methods[0]
    if owners[0].decorator_list or method.decorator_list or len(method.body) != 2:
        return ['semantic manifest requires the reviewed preflight and guarded-read structure']
    capture, protected = method.body
    if (not isinstance(capture, ast.Assign) or len(capture.targets) != 1
            or not isinstance(capture.targets[0], ast.Name) or capture.targets[0].id != 'state'
            or not _same(capture.value, 'self.gateway.authorize(actor, kb_id)')
            or not isinstance(protected, ast.Try)):
        return ['semantic manifest must capture its original authority before storage reads']
    errors = []
    returns = [node for node in ast.walk(method) if isinstance(node, ast.Return)]
    tail = protected.body
    if (len(returns) != 1 or len(tail) < 2 or returns[0] is not tail[-1]
            or not _same(returns[0].value, "(manifest, pointer['sha256'])")):
        errors.append('semantic manifest must have one guarded result exit and no early return')
    guard = 'self.gateway._unchanged(actor, kb_id, state)'
    checks = [node for node in ast.walk(method) if isinstance(node, ast.Call) and _same(node, guard)]
    if (len(checks) != 1 or len(tail) < 2 or not isinstance(tail[-2], ast.Expr)
            or tail[-2].value is not checks[0]):
        errors.append('semantic manifest postflight must be unconditional and immediately precede its result exit')
    if (protected.orelse or protected.finalbody or not protected.handlers
            or any(not handler.body or not isinstance(handler.body[-1], ast.Raise) for handler in protected.handlers)):
        errors.append('semantic manifest failure paths must raise without postflight bypass or finalizer work')
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
           for node in ast.walk(protected)):
        errors.append('semantic manifest postflight cannot be hidden in a deferred nested owner')
    if any(isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
           and node.id in {'state', 'actor', 'kb_id', 'self'} for node in ast.walk(protected)):
        errors.append('semantic manifest must not replace its captured authority or request identity')
    for parent in ast.walk(protected):
        for child in ast.iter_child_nodes(parent):
            if not isinstance(child, ast.Name) or child.id != 'state':
                continue
            read = (isinstance(parent, ast.Subscript) and parent.value is child
                    and isinstance(parent.ctx, ast.Load) and isinstance(parent.slice, ast.Constant)
                    and parent.slice.value in {'manifest_id', 'authority_revision'})
            final_check = len(checks) == 1 and parent is checks[0] and child is parent.args[-1]
            if not read and not final_check:
                errors.append('semantic manifest captured authority must remain read-only until postflight')
    return sorted(set(errors))
