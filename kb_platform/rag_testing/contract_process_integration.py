from pathlib import Path
import sys

from kb_platform.plugin_contracts import ContractApplication, ContractRegistry, Method


def verify_contract_process(root, node=None):
    empty = {'type': 'object', 'additionalProperties': False}
    text = {'type': 'string'}
    registry = ContractRegistry({('application.run', 1): {'run': Method(empty, text)},
                                 ('greeting', 1): {'get': Method(empty, text)}})
    fixture = Path(__file__).resolve().parents[2] / 'examples/contract-application'
    commands = [[sys.executable, '-B', str(fixture / 'worker.py')]]
    if node is not None:
        commands.append([str(node), str(fixture / 'worker.mjs')])
    results = []
    for index, command in enumerate(commands):
        catalog = [
            {'implementation': 'app', 'version': '1', 'command': command,
             'provides': {'server': {'id': 'application.run', 'major': 1}},
             'requires': {'source': {'contract': {'id': 'greeting', 'major': 1}}}},
            {'implementation': 'greeting', 'version': '1', 'command': ['local'],
             'provides': {'value': {'id': 'greeting', 'major': 1}}, 'requires': {}}]
        application = {'version': 1, 'plugins': {
            'entry': {'implementation': 'app', 'version': '1', 'config': {'prefix': 'Hello '}},
            'provider': {'implementation': 'greeting', 'version': '1', 'config': {}}},
            'bindings': {'entry': {'source': {'plugin': 'provider', 'export': 'value'}}},
            'entrypoint': {'plugin': 'entry', 'export': 'server'}}
        calls = []

        class Greeting:
            def get(self, request):
                calls.append(request)
                return 'world'

        def mount(ctx, config):
            ctx.provide('value', Greeting())

        cwd = root / f'contract-process-{index}'
        cwd.mkdir()
        app = ContractApplication(application, catalog, registry=registry,
            mounts={('greeting', '1'): mount}, process_root=cwd,
            schemas={('greeting', '1'): empty, ('app', '1'): {
                'type': 'object', 'properties': {'prefix': text}, 'required': ['prefix'],
                'additionalProperties': False}})
        try:
            result = app.run({})
            if result != 'Hello world' or calls != [{}] or app.host.inspect()['leases']:
                raise RuntimeError('CONTRACT_PROCESS_INTEGRATION_FAILED')
        finally:
            app.close()
        results.append({'result': result, 'dependency_calls': len(calls), 'state': app.host.inspect()['state']})
    return results
