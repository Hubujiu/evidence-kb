from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .plugin_host import PluginHost, PluginSpec
from .plugin_native import PluginError, native_call
from .plugin_process import ProcessService, ProcessTransport


def wire(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError) as error:
        raise PluginError('CONTRACT_VALUE_NOT_SERIALIZABLE') from error


@dataclass(frozen=True)
class Method:
    request: dict
    response: dict

    def __post_init__(self):
        Draft202012Validator.check_schema(self.request)
        Draft202012Validator.check_schema(self.response)

    def validate(self, value, *, response=False):
        selected = wire(value)
        schema = self.response if response else self.request
        error = next(Draft202012Validator(schema).iter_errors(selected), None)
        if error is not None:
            raise PluginError('CONTRACT_RESPONSE_INVALID' if response else 'CONTRACT_REQUEST_INVALID',
                              error.message)
        return selected


class ContractRegistry:
    def __init__(self, contracts):
        self.contracts = dict(contracts)
        for methods in self.contracts.values():
            if not methods or any(not name.isidentifier() or name.startswith('_')
                                  or not isinstance(method, Method) for name, method in methods.items()):
                raise PluginError('INVALID_CONTRACT_METHODS')

    def methods(self, contract):
        key = contract['id'], contract['major']
        if key not in self.contracts:
            raise PluginError('UNKNOWN_SERVICE_CONTRACT', str(key))
        return self.contracts[key]


class ServiceEndpoint:
    def __init__(self, implementation, methods):
        self._implementation, self._methods = implementation, methods
        if any(not callable(getattr(implementation, name, None)) for name in methods):
            raise PluginError('CONTRACT_IMPLEMENTATION_INVALID')

    def __getattr__(self, name):
        if name not in self._methods:
            raise AttributeError(name)
        method = self._methods[name]

        def invoke(request):
            request = method.validate(request)
            result = getattr(self._implementation, name)(request)
            return method.validate(result, response=True)
        return invoke


class Dependency:
    def __init__(self, context, service, methods):
        self._context, self._service, self._methods = context, service, methods

    def __getattr__(self, name):
        if name not in self._methods:
            raise AttributeError(name)
        method = self._methods[name]

        def invoke(request):
            request = method.validate(request)
            binding = dict(self._context.binding(self._service))
            result = self._context.host._invoke(binding, name, (request,), {})
            return method.validate(result, response=True)
        return invoke


class ContractContext:
    def __init__(self, context, definition, manifest, registry):
        self._context, self._definition, self._registry = context, definition, registry
        self._bindings = manifest['config']['bindings']
        self._exports = dict(zip(sorted(definition['provides']), manifest['provides'], strict=True))

    def require(self, alias):
        declaration = self._definition['requires'].get(alias)
        if declaration is None:
            raise PluginError('UNDECLARED_SERVICE_DEPENDENCY', alias)
        if alias not in self._bindings:
            return None
        return Dependency(self._context, self._bindings[alias], self._registry.methods(declaration['contract']))

    def provide(self, export, implementation):
        if export not in self._exports:
            raise PluginError('INVALID_SERVICE_REGISTRATION', export)
        methods = self._registry.methods(self._definition['provides'][export])
        self._context.provide(self._exports[export], ServiceEndpoint(implementation, methods))

    def defer(self, dispose):
        return self._context.defer(dispose)


def _spec(definition, manifest, registry, mount):
    def initialize(context, envelope):
        mount(ContractContext(context, definition, manifest, registry), wire(envelope['configuration']))
    return PluginSpec(manifest['id'], tuple(manifest['provides']), tuple(manifest['requires']), initialize,
                      version=manifest['version'], concurrency=manifest['concurrency'])


def process_mount(definition, registry, *, cwd, journal):
    methods = {name: sorted(registry.methods(contract)) for name, contract in definition['provides'].items()}

    def mount(ctx, config):
        raw = ctx._context
        transport = ProcessTransport(cwd, journal=journal, timeout=300, identity=dict(raw.identity),
            manifest={'command': definition['command'], 'services': methods,
                      'requires': list(definition['requires']), 'configuration': config})
        transport.on_exit = lambda: raw.host._instance_exited(raw.plugin_id,
            raw.identity['instance'], raw.identity['generation'])

        def dependency(call, timeout):
            if len(call['args']) != 1 or call['kwargs']:
                raise PluginError('CONTRACT_REQUEST_INVALID')
            declaration = definition['requires'][call['service']]
            method = registry.methods(declaration['contract']).get(call['method'])
            if method is None:
                raise PluginError('CONTRACT_METHOD_UNKNOWN')
            request = method.validate(call['args'][0])
            service = ctx._bindings.get(call['service'])
            if service is None:
                raise PluginError('CONTRACT_DEPENDENCY_UNAVAILABLE')
            result = raw.host._invoke(dict(raw.binding(service)), call['method'], (request,), {}, timeout=timeout)
            return method.validate(result, response=True)

        transport.invoke_dependency = dependency
        ctx.defer(transport.close)
        transport.start()
        for export, names in methods.items():
            ctx.provide(export, ProcessService(transport, export, tuple(names), raw))
    return mount


class ContractApplication:
    def __init__(self, application, catalog, *, registry, mounts, schemas, process_root=None):
        definitions = deepcopy(catalog)
        by_key = {(item['implementation'], item['version']): item for item in definitions}
        plan = native_call('runtime_compose', application=wire(application), catalog=wire(definitions))
        specs, entries = [], []
        for manifest in plan['plugins']:
            key = manifest['implementation'], manifest['version']
            if key not in schemas or key not in mounts and process_root is None:
                raise PluginError('CONTRACT_EXECUTOR_MISSING', str(key))
            definition = by_key[key]
            for contract in definition['provides'].values():
                registry.methods(contract)
            for declaration in definition['requires'].values():
                registry.methods(declaration['contract'])
            schema = schemas[key]
            Draft202012Validator.check_schema(schema)
            error = next(Draft202012Validator(schema).iter_errors(manifest['config']['configuration']), None)
            if error is not None:
                raise PluginError('INVALID_PLUGIN_CONFIG', error.message)
            mount = mounts.get(key)
            if mount is None:
                root = Path(process_root).resolve()
                mount = process_mount(definition, registry, cwd=root,
                    journal=root / '.plugin-journal' / manifest['id'])
            specs.append(_spec(definition, manifest, registry, mount))
            entries.append({'id': manifest['id'], 'use': manifest['id'], 'config': manifest['config']})
        self.plan = plan
        self.host = PluginHost({'version': 1, 'plugins': entries}, specs)

    def run(self, request):
        return self.host.call(self.plan['entrypoint'], 'run', wire(request))

    def stop(self):
        self.host.drain()
        with self.host._lock:
            operations = tuple(self.host._cancellations)
        for operation in operations:
            try:
                self.host.cancel(operation)
            except PluginError as error:
                if error.code != 'unknown_operation':
                    raise

    def close(self):
        self.host.close()
