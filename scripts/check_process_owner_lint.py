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
             'scripts/check_process_owner_linux.py',
             'kb_platform/rag_testing/foundation_integration.py')
    sources = sources or {path: (Path(root) / path).read_text('utf-8') for path in files}
    errors = []
    parsed = {path: nodes(sources[path]) for path in files}
    main = parsed[files[0]].get('main')
    if main is None or not contains(main,
            'from scripts.check_process_owner_lint import violations as process_owner_violations') or not contains(
            main, 'violations.extend(process_owner_violations(ROOT))'):
        errors.append('Project entry must enforce process ownership lint')
    gate = parsed[files[0]].get('run_check')
    if gate is None or not contains(gate, 'from scripts.check_process_owner import run_owned') or not contains(
            gate, 'result = run_owned(command, cwd=ROOT, env=env, stream=stream, timeout=timeout)') or not contains(
            gate, 'code = 0 if result.passed else result.exit_code or 2'):
        errors.append('Every run_check must use owned execution and preserve failure')
    if gate is not None and any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess'
            and node.func.attr in ('Popen', 'run', 'call', 'check_call', 'check_output')
            for node in ast.walk(gate)):
        errors.append('run_check cannot bypass process ownership')
    common = parsed[files[1]]
    expected = ast.parse('def passed(self):\n'
        '    return (self.exit_code == 0 and not self.timed_out and not self.cancelled\n'
        '            and self.cleanup_complete and self.error is None)').body[0]
    actual = common.get('passed')
    if actual is None or ast.dump(actual.body[0]) != ast.dump(expected.body[0]):
        errors.append('PASS requires exit success, sticky failure facts and complete cleanup')
    caller = common.get('run_owned')
    startup = common.get('_main')
    unsupported_caller_guard = ast.parse("if sys.platform != 'linux':\n"
        "    return Outcome(None, False, bool(cancel_event and cancel_event.is_set()), False, 0,\n"
        "                   'UNSUPPORTED_PROCESS_CONTAINMENT')").body[0]
    if caller is None or not caller.body or ast.dump(caller.body[0]) != ast.dump(unsupported_caller_guard):
        errors.append('Non-Linux caller must refuse process containment before launch')
    if caller is None or not contains(caller,
            "config = dict(command=command, cwd=str(cwd), timeout=timeout, cleanup_timeout=cleanup_timeout, result=str(result_path))") or not contains(
            caller, 'supervisor_env.pop(_CONFIG_ENV, None)'):
        errors.append('Supervisor startup metadata must exclude payload environment and be removed after launch')
    if caller is not None and any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ('write', 'flush') and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == 'stdin' for node in ast.walk(caller)):
        errors.append('Supervisor control pipe must never block on configuration writes')
    if startup is None or not contains(startup, 'config_text = os.environ.pop(_CONFIG_ENV)'):
        errors.append('Supervisor must remove startup metadata before launching payload')
    supervisor = common.get('supervise')
    unsupported_supervisor_guard = ast.parse("if sys.platform != 'linux':\n"
        "    return Outcome(None, False, cancelled.is_set(), False, 0,\n"
        "                   'UNSUPPORTED_PROCESS_CONTAINMENT')").body[0]
    if supervisor is None or not supervisor.body or ast.dump(supervisor.body[0]) != ast.dump(unsupported_supervisor_guard):
        errors.append('Non-Linux supervisor must refuse process containment before launch')
    if supervisor is None or not any(isinstance(node, ast.Try) and node.finalbody
            and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == 'cleanup' for statement in node.finalbody for call in ast.walk(statement))
            for node in ast.walk(supervisor)):
        errors.append('Supervisor must always attempt owned cleanup in finally')
    if supervisor is None or not contains(supervisor,
            "if owner is not None and cleanup_finished_monotonic > cleanup_deadline:\n    error = error or 'CLEANUP_DEADLINE_EXCEEDED'"):
        errors.append('Cleanup completed after its deadline must remain a failure')
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
    for path in files[:3]:
        source = sources[path]
        if any(value in source for value in ('taskkill', 'pkill', 'killall', "Path('/proc').", 'os.kill(')):
            errors.append('Only owned handles may receive process termination')
    foundation = ast.parse(sources[files[3]])
    suites = [node for node in ast.walk(foundation) if isinstance(node, ast.For)
              and isinstance(node.iter, ast.Tuple)]
    if not any(sum(isinstance(item, ast.Name) and item.id == 'check_process_owner_integration'
                   for item in node.iter.elts) == 1 for node in suites):
        errors.append('Actual process ownership fixtures must remain mandatory foundation integration')
    return errors
