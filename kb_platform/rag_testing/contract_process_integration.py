from pathlib import Path
import json
import os
import sys

from kb_platform.plugin_contracts import ContractApplication, ContractError, ContractRegistry, Method


def verify_contract_process(root, node=None):
    empty = {'type': 'object', 'additionalProperties': False}
    text = {'type': 'string'}
    registry = ContractRegistry({('application.run', 1): {'run': Method(empty, text)},
                                 ('greeting', 1): {'get': Method(empty, text)}})
    fixture = Path(__file__).resolve().parents[2] / 'examples/contract-application'
    commands = [[sys.executable, '-B', str(fixture / 'worker.py')]]
    if node is not None:
        commands.append([str(node), str(fixture / 'worker.mjs')])
    rust = Path(os.environ['KB_CONTRACT_PROVIDER']).resolve(strict=True)
    results = []
    cases = [(command, mode) for command in commands for mode in ('local', 'rust', 'rust-error')]
    for index, (command, mode) in enumerate(cases):
        catalog = [
            {'implementation': 'app', 'version': '1', 'command': command,
             'provides': {'server': {'id': 'application.run', 'major': 1}},
             'requires': {'source': {'contract': {'id': 'greeting', 'major': 1}}}},
            {'implementation': 'greeting', 'version': '1', 'executor': 'local' if mode == 'local' else 'process',
             'command': [] if mode == 'local' else [str(rust)],
             'provides': {'value': {'id': 'greeting', 'major': 1}}, 'requires': {}}]
        application = {'version': 1, 'plugins': {
            'entry': {'implementation': 'app', 'version': '1', 'config': {'prefix': 'Hello '}},
            'provider': {'implementation': 'greeting', 'version': '1',
                         'config': {'error': True} if mode == 'rust-error' else {}}},
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
            mounts={('greeting', '1'): mount} if mode == 'local' else {}, process_root=cwd,
            schemas={('greeting', '1'): {'type': 'object', 'properties': {'error': {'type': 'boolean'}},
                                       'additionalProperties': False}, ('app', '1'): {
                'type': 'object', 'properties': {'prefix': text}, 'required': ['prefix'],
                'additionalProperties': False}})
        transports = [app.host.service(app.plan['exports']['entry']['server'])._implementation.transport]
        if mode != 'local':
            transports.append(app.host.service(app.plan['exports']['provider']['value'])._implementation.transport)
            if transports[-1].runtime_info['language'] != 'rust':
                raise RuntimeError('CONTRACT_RUST_PROVIDER_NOT_EXECUTED')
        result = error = None
        try:
            try:
                result = app.run({})
            except ContractError as failure:
                if mode != 'rust-error':
                    raise
                error = failure.to_dict()
            expected_error = {'code': 'CONTRACT_PROVIDER_UNAVAILABLE', 'message': 'Greeting provider unavailable',
                              'status_code': 503, 'details': {'retryable': True}}
            if (mode == 'rust-error' and error != expected_error
                    or mode != 'rust-error' and result != 'Hello world'
                    or calls != ([{}] if mode == 'local' else []) or app.host.inspect()['leases']):
                raise RuntimeError('CONTRACT_PROCESS_INTEGRATION_FAILED')
        finally:
            app.close()
        rows = [json.loads(path.read_text('utf-8')) for path in (cwd / '.plugin-journal/entry').glob('*.json')]
        callbacks = [row for row in rows if row['request'].get('kind') == 'dependency_response'
                     and row['status'] in {'COMPLETE', 'ERROR'}]
        if (len(callbacks) != 1 or callbacks[0]['status'] != ('ERROR' if mode == 'rust-error' else 'COMPLETE')
                or any(transport._process.poll() is None or transport._reader.is_alive()
                       or transport._writer.is_alive() for transport in transports)
                or app.host.inspect()['state'] != 'disposed'):
            raise RuntimeError('CONTRACT_PROCESS_CLEANUP_FAILED')
        results.append({'result': result, 'error': error, 'provider': mode,
                        'consumer': transports[0].runtime_info['language'],
                        'dependency_calls': len(callbacks), 'state': app.host.inspect()['state']})
    return results
