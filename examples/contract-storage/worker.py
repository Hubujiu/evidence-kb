import importlib.util
import json
from pathlib import Path
import sys


package = Path(__file__).resolve().parents[2] / 'kb_platform'
specification = importlib.util.spec_from_file_location('kb_platform', package / '__init__.py',
                                                       submodule_search_locations=[str(package)])
module = importlib.util.module_from_spec(specification)
sys.modules[specification.name] = module
specification.loader.exec_module(module)

storage_module = importlib.import_module('kb_platform.application_storage')
contracts_module = importlib.import_module('kb_platform.plugin_contracts')
ApplicationStorage, storage_methods = storage_module.ApplicationStorage, storage_module.storage_methods
ContractError, ServiceEndpoint = contracts_module.ContractError, contracts_module.ServiceEndpoint


PROTOCOL = 'evidence-plugin-process/1'
storage = None


def send(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


try:
    for line in sys.stdin:
        request = json.loads(line)
        kind = request['kind']
        try:
            result = None
            if kind == 'handshake':
                storage = ApplicationStorage(request['configuration'])
                methods = storage_methods()
                endpoint = ServiceEndpoint(storage, methods)
                result = {'protocol': PROTOCOL, 'services': {'storage': sorted(methods)},
                          'runtime': {'language': 'python', 'version': sys.version, 'executable': sys.executable}}
            elif kind == 'invoke':
                result = getattr(endpoint, request['method'])(*request['args'], **request['kwargs'])
            elif kind == 'cancel':
                continue
            elif kind == 'shutdown':
                storage.close()
                storage = None
            send({'protocol': PROTOCOL, 'id': request['id'], 'ok': True, 'result': result})
        except ContractError as error:
            send({'protocol': PROTOCOL, 'id': request['id'], 'ok': False, 'error': error.to_dict()})
        if kind == 'shutdown':
            break
finally:
    if storage is not None:
        storage.close()
