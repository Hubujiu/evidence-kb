import ast
import re
from pathlib import Path

from .formal_state_machines import python_symbols


REQUIRED = {
    'idle_exit_dispatcher_settled': ["return transport._closed and disposed.is_set() and (not snapshot['leases']) and (not any((snapshot['counts'][key] for key in ('resources', 'operations', 'pending_operations', 'queued_actions')))) and (not transport._writer.is_alive()) and (not transport._reader.is_alive()) and (transport._process.poll() is not None)"],
    'idle_exit_case': ['completed = wait_until(lambda: idle_exit_dispatcher_settled(host, transport, disposed), 3)'],
    'PluginHost._pump': [
        'if root._pump_owner is not None or root._final_snapshot is not None:\n    return []',
        'if root._pump_owner is not owner:\n    raise PluginError("PLUGIN_LIFECYCLE_BUSY")',
        'if root._pump_owner is owner:\n    root._pump_owner = None'],
    'PluginHost._release': ['authority.users -= 1', 'settled = not authority.users',
                            'if settled:\n    self._settle(authority)'],
    'PluginHost._invoke': ['self._cancellations.pop(action["operation"], None)',
                          'if timer_started:\n    timer.join()', 'self._release(invocation)'],
    'PluginHost.update': ['if self._entries.get(plugin) is not entry:\n    continue',
                          'self._entries[plugin] = spec, selected_config'],
    'PluginHost._collect': ['if self._final_snapshot is None:\n    for child in tuple(self._children.values()):\n        child._collect()\n    self._collection = native_call("runtime_collect", host=self.id)\n    self._final_snapshot = self._collection["snapshot"]'],
    'HttpApplication._serve': ['self._entered = True', 'self._finished = self._entered'],
    'api': ['asyncio.run(app.router._shutdown())'],
    'delivery': ['asyncio.run(app.router._shutdown())'],
    'HttpApplication.close': ['if not self._finished:\n    asyncio.run(self.app.router._shutdown())\n    self._finished = True'],
}


def dump(node):
    return ast.dump(node, include_attributes=False)


def ownership_violations(root: Path) -> list[str]:
    plugin = python_symbols((root / 'kb_platform/plugin_host.py').read_text('utf-8'))
    http = python_symbols((root / 'kb_platform/host_applications.py').read_text('utf-8'))
    process = python_symbols((root / 'kb_platform/rag_testing/plugin_process_integration.py').read_text('utf-8'))
    symbols = {**plugin, **http, **process}
    errors = []
    for name, fragments in REQUIRED.items():
        nodes = {dump(node) for node in ast.walk(symbols[name])}
        for fragment in fragments:
            if dump(ast.parse(fragment).body[0]) not in nodes:
                errors.append(f'python host ownership: {name} reviewed guard changed: {fragment.splitlines()[0]}')
    for name, release in [('PluginHost.lease', 'self._release(authority)'),
                          ('PluginContext.lease', 'self.host._release(lease)')]:
        valid = False
        for node in ast.walk(symbols[name]):
            if not isinstance(node, ast.Try):
                continue
            body = ast.Module(body=node.body, type_ignores=[])
            cleanup = ast.Module(body=node.finalbody, type_ignores=[])
            sets = [n for n in ast.walk(body) if isinstance(n, ast.Call)
                    and dump(n.func) == dump(ast.parse('_leases.set', mode='eval').body)]
            releases = any(dump(n) == dump(ast.parse(release).body[0]) for n in ast.walk(cleanup))
            guarded = any(dump(n) == dump(ast.parse('if token is not None:\n    _leases.reset(token)').body[0])
                          for n in ast.walk(cleanup))
            valid |= bool(sets and releases and guarded and any(isinstance(n, ast.Yield) for n in ast.walk(body)))
        if not valid:
            errors.append(f'python host ownership: {name} context setup must be inside releasing finally')
    execute = symbols['PluginHost._execute']
    callbacks = [node for node in ast.walk(execute) if isinstance(node, ast.Try)
                 and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'callback'
                         for n in ast.walk(ast.Module(body=node.body, type_ignores=[])))]
    if len(callbacks) != 1 or any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                  and n.func.attr == 'pop' for n in ast.walk(ast.Module(body=callbacks[0].finalbody, type_ignores=[]))):
        errors.append('python host ownership: failed disposer must retain callback for explicit retry')
    lease_model = (root / 'formal/python_host_lifecycles/PythonLease.tla').read_text('utf-8')
    errors.extend(lease_witness_violations(lease_model))
    foundation = (root / 'kb_platform/rag_testing/foundation_integration.py').read_text('utf-8')
    if 'foundation_transport_integration, http_lifespan_integration' not in foundation:
        errors.append('python host ownership: actual HTTP lifespan cases require mandatory foundation stage')
    run_nodes = {dump(node) for node in ast.walk(process['run'])}
    for fragment in ["dispatcher = None if blocked_only else verify_idle_exit_dispatcher(manager, packages['process-supervision'], root)",
                     "if dispatcher is not None and dispatcher['decision'] != 'PASS':\n    raise RuntimeError('PROCESS_DISPATCHER_OBSERVATION_CONTRACT_FAILED')"]:
        if dump(ast.parse(fragment).body[0]) not in run_nodes:
            errors.append('python host ownership: mandatory dispatcher barrier contract changed')
    return errors


def lease_witness_violations(source: str) -> list[str]:
    compact = re.sub(r"\s+", "", source)
    fragments = [
        'vars == <<phase,users,native,token,signal,timer,settled,badRelease,failedSetup,nestedSeen,invokedSeen>>',
        'nestedSeen = FALSE /\\ invokedSeen = FALSE',
        'Nest == /\\ phase = "body" /\\ users = 1 /\\ phase\' = "nested" /\\ users\' = 2 /\\ nestedSeen\' = TRUE',
        'Invoke == /\\ phase \\in {"body","body-end"} /\\ phase\' = "invoking" /\\ signal\' = TRUE /\\ timer\' = "started" /\\ invokedSeen\' = TRUE',
        'NestedInvocationSettledUnreachable == ~(phase = "done" /\\ nestedSeen /\\ invokedSeen /\\ ~failedSetup /\\ timer = "stopped" /\\ settled = 1)',
    ]
    errors = ["python host ownership: nested invocation witness must require actual Nest and Invoke"
              for fragment in fragments if re.sub(r"\s+", "", fragment) not in compact]
    if compact.count("invokedSeen'=") != 1 or compact.count("nestedSeen'=") != 1:
        errors.append("python host ownership: witness history may only be set by its actual event")
    actions = re.split(r'(?m)^(\w+)(?:\([^\n]*\))?\s*==', source)
    bodies = dict(zip(actions[1::2], actions[2::2]))
    for name in ('Acquire', 'Setup', 'Nest', 'NestedExit', 'TimerFire', 'TimerDone', 'Finish', 'Release'):
        unchanged = re.findall(r'UNCHANGED\s*<<([^>]*)>>', bodies.get(name, ''))
        if not any('invokedSeen' in re.findall(r'\w+', fields) for fields in unchanged):
            errors.append(f'python host ownership: {name} must preserve invocation history')
    return errors
