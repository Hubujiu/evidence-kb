import ast


def _same(node, expression):
    return isinstance(node, ast.AST) and ast.dump(node, include_attributes=False) == ast.dump(
        ast.parse(expression, mode='eval').body, include_attributes=False)


def knowledge_workflow_violations(source):
    tree = ast.parse(source)
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'KnowledgeTurn']
    methods = [node for owner in owners for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == 'run']
    if len(owners) != 1 or len(methods) != 1 or owners[0].decorator_list or methods[0].decorator_list:
        return ['knowledge workflow result owner is missing, decorated or ambiguous']
    method = methods[0]
    protected = [node for node in method.body if isinstance(node, ast.Try)]
    if len(protected) != 1:
        return ['knowledge workflow requires one outer failure boundary']
    protected = protected[0]
    executions = [node for node in protected.body if isinstance(node, ast.Assign)
                  and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == 'response' and isinstance(node.value, ast.Call)
                  and _same(node.value.func, 'self.service.runtime.run')]
    if len(executions) != 1 or protected.orelse or protected.finalbody:
        return ['knowledge workflow result must be captured once inside the failure boundary']
    tail = protected.body[protected.body.index(executions[0]) + 1:]
    if len(tail) < 3 or not isinstance(tail[0], ast.Expr) or not _same(tail[0].value, 'self.check_execution()'):
        return ['knowledge workflow result requires its final execution check']
    guard = tail[1]
    if (not isinstance(guard, ast.If) or not _same(guard.test, "response['status'] == 'error'")
            or guard.orelse or len(guard.body) != 1 or not isinstance(guard.body[0], ast.Raise)
            or guard.body[0].cause is not None or not _same(guard.body[0].exc,
                "PlatformError(response['stop_reason'], 'Knowledge workflow failed', 503)")):
        return ['knowledge workflow business error must exit before success publication and retain its reason']
    success = [node for node in ast.walk(method) if isinstance(node, ast.Call)
               and _same(node.func, 'self.service.store.finish')]
    if (len(success) != 1 or not isinstance(tail[-1], ast.Return)
            or tail[-1].value is not success[0]):
        return ['knowledge workflow must have one guarded success publication exit']
    if any(isinstance(node, ast.Return) for statement in tail[:-1] for node in ast.walk(statement)):
        return ['knowledge workflow cannot return a result before guarded publication']
    for statement in tail[2:]:
        for node in ast.walk(statement):
            if (isinstance(node, ast.Name) and node.id == 'response' and isinstance(node.ctx, ast.Store)):
                return ['knowledge workflow cannot replace its classified result before publication']
            if (isinstance(node, ast.Subscript) and _same(node.value, 'response')
                    and isinstance(node.ctx, (ast.Store, ast.Del))):
                return ['knowledge workflow cannot mutate its classified result before publication']
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _same(node.func.value, 'response'):
                if node.func.attr != 'get':
                    return ['knowledge workflow result access must remain read-only after classification']
    handlers = protected.handlers
    if len(handlers) != 1 or not _same(handlers[0].type, 'Exception') or handlers[0].name != 'exc':
        return ['knowledge workflow business errors require the existing exception failure boundary']
    handler = handlers[0]
    returns = [node for node in ast.walk(handler) if isinstance(node, ast.Return)]
    if (len(returns) != 1 or returns[0] is not handler.body[-1]
            or not isinstance(returns[0].value, ast.Call)
            or not _same(returns[0].value.func, 'self.service.store.fail')):
        return ['knowledge workflow failure boundary must publish exactly one failure, not success']
    call = returns[0].value
    failures = [node for node in ast.walk(method) if isinstance(node, ast.Call)
                and _same(node.func, 'self.service.store.fail')]
    if failures != [call]:
        return ['knowledge workflow must not publish a second terminal failure']
    if len(call.args) != 5 or not isinstance(call.args[-1], ast.Dict):
        return ['knowledge workflow failure publication must use its explicit redacted result']
    result = {key.value: value for key, value in zip(call.args[-1].keys, call.args[-1].values)
              if isinstance(key, ast.Constant)}
    for key, value in {'answer': "''", 'claims': '[]', 'evidence': '[]', 'status': "'error'",
                       'completion': "'error'", 'stop_reason': 'code'}.items():
        if key not in result or not _same(result[key], value):
            return ['knowledge workflow failed result must remain redacted and preserve the failure code']
    return []
