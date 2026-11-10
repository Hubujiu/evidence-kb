



import ast
from pathlib import Path


SOURCE = 'kb_platform/rag_testing/delivery_http.py'
GUARD = "sqlite_guard(root / '.prepare.guard.sqlite3', nowait=True)"


def acceptance_workflow_violations(root):
    errors = []
    try:
        tree = ast.parse((Path(root) / SOURCE).read_text('utf-8'))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        guarded = {}
        for name, callee in (('prepare_http', '_prepare_http_locked'), ('rebind_http', '_bind_prepared_http')):
            function = functions[name]
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == callee]
            owned = [node for block in ast.walk(function) if isinstance(block, ast.With)
                     and len(block.items) == 1 and ast.unparse(block.items[0].context_expr) == GUARD
                     for node in ast.walk(block) if node in calls]
            if len(calls) != 1 or owned != calls:
                errors.append(f'acceptance_workflow: {name} must invoke {callee} under the persistent output guard')
            if name == 'rebind_http':
                blocks = [node for node in ast.walk(function) if isinstance(node, ast.With)
                          and len(node.items) == 1 and ast.unparse(node.items[0].context_expr) == GUARD]
                expected = ast.unparse(ast.parse("any(path.name not in {'.prepare.guard.sqlite3', "
                    "'.prepare.guard.sqlite3-journal', '.prepare.guard.sqlite3-wal', "
                    "'.prepare.guard.sqlite3-shm'} for path in root.iterdir())", mode='eval').body)
                if (len(blocks) != 1 or len(blocks[0].body) != 2
                        or not isinstance(blocks[0].body[0], ast.If)
                        or ast.unparse(blocks[0].body[0].test) != expected
                        or [ast.unparse(node) for node in blocks[0].body[0].body] != ['raise FileExistsError(root)']):
                    errors.append('acceptance_workflow: rebind must revalidate the empty destination after acquiring ownership')
            guarded[name] = calls

        all_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == '_prepare_http_locked']
        if all_calls != guarded['prepare_http']:
            errors.append('acceptance_workflow: unguarded preparation entrypoint')
        once = next(node for node in functions['_prepare_http_locked'].body
                    if isinstance(node, ast.FunctionDef) and node.name == 'once')
        statements = [ast.unparse(node) for node in once.body]
        first = once.body[0]
        if (not isinstance(first, ast.If) or ast.unparse(first.test) != "key in state['operations']"
                or len(first.body) != 3 or not isinstance(first.body[1], ast.If)
                or ast.unparse(first.body[1].test) != "record['status'] != 'COMPLETE'"
                or not any(isinstance(node, ast.Raise) for node in first.body[1].body)
                or ast.unparse(first.body[2]) != "return deepcopy(record['response'])"):
            errors.append('acceptance_workflow: only COMPLETE receipts may be reused; every other existing receipt must block')
        reservation = "record = {'method': method, 'path': url, 'status': 'STARTED'}"
        publication = "state['operations'][key] = record"
        persist = '_atomic_json(path, state)'
        try_index = next(index for index, node in enumerate(once.body) if isinstance(node, ast.Try))
        if statements[1:try_index] != ['require_no_pending(physical_directory)', reservation, publication, persist]:
            errors.append('acceptance_workflow: STARTED must be published before the request')
        attempt = once.body[try_index]
        if not attempt.body or ast.unparse(attempt.body[0]) != 'response = client.request(method, url, **kwargs)':
            errors.append('acceptance_workflow: request moved outside its persisted claim/exception boundary')
        sends = [node for node in ast.walk(once) if isinstance(node, ast.Call)
                 and ast.unparse(node.func) == 'client.request']
        if len(sends) != 1:
            errors.append('acceptance_workflow: preparation operation must have exactly one send site')

        locked = ast.unparse(functions['_prepare_http_locked'])
        if ("value['status'] != 'COMPLETE'" not in locked or
                "raise RuntimeError('PREPARE_OPERATION_REQUIRES_RECONCILIATION:' + ','.join(pending))" not in locked):
            errors.append('acceptance_workflow: reload must reject unresolved preparation operations')
    except (OSError, SyntaxError, KeyError, StopIteration, IndexError, ValueError) as error:
        errors.append('acceptance_workflow: invalid ownership structure: ' + type(error).__name__)
    return errors
