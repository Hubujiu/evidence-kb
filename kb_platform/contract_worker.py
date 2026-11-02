from pathlib import Path

from .host_protocol import HostProtocolError
from .host_worker import ApplicationWorker, main
from .plugin_contracts import ContractApplication, ContractRegistry, Method


class Application:
    def __init__(self, document):
        if not isinstance(document, dict) or set(document) != {
                'application', 'catalog', 'contracts', 'schemas', 'request', 'root'}:
            raise HostProtocolError('HOST_INVALID_CONTRACT_APPLICATION')
        contracts = {}
        for contract in document['contracts']:
            if set(contract) != {'id', 'major', 'methods'}:
                raise HostProtocolError('HOST_INVALID_CONTRACT')
            key = contract['id'], contract['major']
            if key in contracts:
                raise HostProtocolError('HOST_DUPLICATE_CONTRACT')
            contracts[key] = {name: Method(**method) for name, method in contract['methods'].items()}
        schemas = {}
        for schema in document['schemas']:
            if set(schema) != {'implementation', 'version', 'schema'}:
                raise HostProtocolError('HOST_INVALID_CONFIGURATION_SCHEMA')
            key = schema['implementation'], schema['version']
            if key in schemas:
                raise HostProtocolError('HOST_DUPLICATE_CONFIGURATION_SCHEMA')
            schemas[key] = schema['schema']
        self.request = document['request']
        self.application = ContractApplication(document['application'], document['catalog'],
            registry=ContractRegistry(contracts), mounts={}, schemas=schemas,
            process_root=Path(document['root']).resolve())

    def run(self):
        return self.application.run(self.request)

    def close(self):
        self.application.close()

    def stop(self):
        self.application.stop()


class Worker(ApplicationWorker):
    def _boot(self, params):
        self._application = Application(params)
        with self._lock:
            self._phase = 'ready'
        return {'ready': True}


if __name__ == '__main__':
    raise SystemExit(main(Worker))
