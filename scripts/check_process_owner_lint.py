import ast
from pathlib import Path


def nodes(source):
    return {node.name: node for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def contains(node, source):
    required = ast.parse(source).body[0]
    expected = ast.dump(required, include_attributes=False)
    return any(ast.dump(child, include_attributes=False) == expected for child in ast.walk(node))


def violations(root, sources=None):
    files = ('scripts/check_project.py', 'scripts/check_process_owner.py',
             'scripts/check_process_owner_linux.py', 'scripts/check_process_owner_windows.py',
             'kb_platform/rag_testing/foundation_integration.py')
    sources = sources or {path: (Path(root) / path).read_text('utf-8') for path in files}
    errors = []
    parsed = {path: nodes(sources[path]) for path in files}
    common = parsed[files[1]]
    expected = ast.parse('def passed(self):\n'
        '    return (self.exit_code == 0 and not self.timed_out and not self.cancelled\n'
        '            and self.cleanup_complete and self.error is None)').body[0]
    actual = common.get('passed')
    if actual is None or ast.dump(actual.body[0]) != ast.dump(expected.body[0]):
        errors.append('PASS requires exit success, sticky failure facts and complete cleanup')
    supervisor = common.get('supervise')
    if supervisor is None or not any(isinstance(node, ast.Try) and node.finalbody
            and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == 'cleanup' for statement in node.finalbody for call in ast.walk(statement))
            for node in ast.walk(supervisor)):
        errors.append('Supervisor must always attempt owned cleanup in finally')
    linux = parsed[files[2]]
    cleanup = linux.get('cleanup')
    if cleanup is None or not contains(cleanup, 'pid, status = os.waitpid(-1, os.WNOHANG)') or not any(
            isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name)
            and node.type.id == 'ChildProcessError' and len(node.body) == 1
            and contains(node, 'return not self.failed') for node in ast.walk(cleanup)):
        errors.append('Linux emptiness must come from ECHILD, not a racy children listing')
    bind = linux.get('bind')
    if bind is None or not contains(bind, "if parent != os.getpid():\n    raise RuntimeError('NOT_SUPERVISOR_CHILD')") or not contains(bind,
            "if previous[0] != generation:\n    raise RuntimeError('CHILD_PID_REUSED')"):
        errors.append('Linux handles require own-child and generation checks')
    windows = parsed[files[3]]
    spawn = windows.get('spawn')
    if spawn is None:
        errors.append('Windows owner must expose contained spawn')
    else:
        calls = [node for node in ast.walk(spawn) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr in ('create_process', 'assign', 'resume')]
        calls.sort(key=lambda node: (node.lineno, node.col_offset))
        if [node.func.attr for node in calls] != ['create_process', 'assign', 'resume']:
            errors.append('Windows must create suspended, assign Job, then resume')
        if not contains(spawn, 'self._api.assign(self._job, self._process)') or not contains(spawn,
                'self._api.resume(self._thread)'):
            errors.append('Windows must bind the exact process and resume the retained thread handle')
    window_cleanup = windows.get('cleanup')
    if window_cleanup is None or not contains(window_cleanup, 'if empty and exited:\n    return True'):
        errors.append('Windows cleanup requires empty Job and completed direct process')
    for path in files[:4]:
        source = sources[path]
        if any(value in source for value in ('taskkill', 'pkill', 'killall', "Path('/proc').", 'os.kill(')):
            errors.append('Only owned handles may receive process termination')
    foundation = ast.parse(sources[files[4]])
    suites = [node for node in ast.walk(foundation) if isinstance(node, ast.For)
              and isinstance(node.iter, ast.Tuple)]
    if not any(sum(isinstance(item, ast.Name) and item.id == 'check_process_owner_integration'
                   for item in node.iter.elts) == 1 for node in suites):
        errors.append('Actual process ownership fixtures must remain mandatory foundation integration')
    return errors
