from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType

from jsonschema import Draft202012Validator

from .plugin_host import PluginHost, PluginSpec
from .plugin_native import PluginError, native_call
from .plugin_process import ProcessService, ProcessTransport


def wire(value):
    def check(item, depth=0):
        if depth > 64:
            raise ValueError('JSON nesting limit exceeded')
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise TypeError('JSON object keys must be strings')
            for entry in item.values():
                check(entry, depth + 1)
        elif type(item) is list:
            for entry in item:
                check(entry, depth + 1)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise TypeError('Expected a JSON value')

    try:
        check(value)
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(serialized.encode('utf-8')) > 4 * 1024 * 1024:
            raise ValueError('JSON message limit exceeded')
        return json.loads(serialized)
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise PluginError('CONTRACT_VALUE_NOT_SERIALIZABLE') from error


def schema_validator(schema):
    selected = wire(schema)

    def local_references(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key in {'$ref', '$dynamicRef', '$id'} and (
                        not isinstance(value, str) or not value.startswith('#')):
                    raise PluginError('CONTRACT_EXTERNAL_SCHEMA_REFERENCE')
                local_references(value)
        elif isinstance(item, list):
            for value in item:
                local_references(value)

    local_references(selected)
    Draft202012Validator.check_schema(selected)
    return Draft202012Validator(selected)


class ContractError(PluginError):
    def __init__(self, code, message, *, status_code=500, details=None):
        if (not isinstance(code, str) or not code or not isinstance(message, str)
                or type(status_code) is not int or not 400 <= status_code <= 599):
            raise PluginError('INVALID_CONTRACT_ERROR')
        super().__init__(code, message)
        self.status_code, self.details = status_code, wire({} if details is None else details)
        if not isinstance(self.details, dict):
            raise PluginError('INVALID_CONTRACT_ERROR')

    def to_dict(self):
        return {'code': self.code, 'message': str(self), 'status_code': self.status_code, 'details': self.details}


def remote_error(error):
    value = getattr(error, 'remote_error', None)
    if (error.code == 'PROCESS_CALL_ERROR' and isinstance(value, dict)
            and {'code', 'message'} <= set(value) <= {'code', 'message', 'status_code', 'details'}):
        return ContractError(value['code'], value['message'],
            status_code=value.get('status_code', 500), details=value.get('details'))
    return error


class Method:
    def __init__(self, request, response):
        self._request, self._response = wire(request), wire(response)
        self._request_validator = schema_validator(self._request)
        self._response_validator = schema_validator(self._response)

    @property
    def request(self):
        return deepcopy(self._request)

    @property
    def response(self):
        return deepcopy(self._response)

    def validate(self, value, *, response=False):
        selected = wire(value)
        validator = self._response_validator if response else self._request_validator
        error = next(validator.iter_errors(selected), None)
        if error is not None:
            raise PluginError('CONTRACT_RESPONSE_INVALID' if response else 'CONTRACT_REQUEST_INVALID',
                              error.message)
        return selected


class ContractRegistry:
    def __init__(self, contracts):
        selected = {}
        for key, methods in contracts.items():
            if (not isinstance(key, tuple) or len(key) != 2 or not isinstance(key[0], str)
                    or not key[0] or type(key[1]) is not int or key[1] < 1):
                raise PluginError('INVALID_SERVICE_CONTRACT')
            if not isinstance(methods, dict) or not methods or any(not isinstance(name, str)
                                  or not name.isidentifier() or name.startswith('_')
                                  or not isinstance(method, Method) for name, method in methods.items()):
                raise PluginError('INVALID_CONTRACT_METHODS')
            selected[key] = MappingProxyType({name: Method(method.request, method.response)
                                              for name, method in methods.items()})
        self.contracts = MappingProxyType(selected)

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
            try:
                result = getattr(self._implementation, name)(request)
            except ContractError:
                raise
            except PluginError as error:
                converted = remote_error(error)
                if converted is error:
                    raise
                raise converted from error
            except Exception as error:
                code = getattr(error, 'code', None)
                status = getattr(error, 'status_code', 500)
                if isinstance(code, str) and code and type(status) is int and 400 <= status <= 599:
                    details = wire(getattr(error, 'details', {}))
                    if not isinstance(details, dict):
                        details = {}
                    if hasattr(error, 'call_record'):
                        details['call_record'] = wire(error.call_record)
                    raise ContractError(code, str(error), status_code=status, details=details) from error
                raise ContractError('CONTRACT_IMPLEMENTATION_FAILED', 'Service implementation failed') from error
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
            with self._context.lease():
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

    @property
    def identity(self):
        return dict(self._context.identity)

    @property
    def cancelled(self):
        call = self._context.host.current_call
        return call is not None and call['cancel_signal'].is_set()

    def wait_cancelled(self, timeout=None):
        call = self._context.host.current_call
        if call is None:
            raise PluginError('CONTRACT_CALL_CONTEXT_REQUIRED')
        return call['cancel_signal'].wait(timeout)


def _spec(definition, manifest, registry, mount):
    def initialize(context, envelope):
        mount(ContractContext(context, definition, manifest, registry), wire(envelope['configuration']))
    identity = json.dumps([manifest['id'], manifest['implementation'], manifest['version']], separators=(',', ':'))
    key = 'contract.' + sha256(identity.encode('utf-8')).hexdigest()
    return PluginSpec(key, tuple(manifest['provides']), tuple(manifest['requires']), initialize,
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
            with raw.lease():
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
        specs, entries, methods = [], [], {}
        for manifest in plan['plugins']:
            key = manifest['implementation'], manifest['version']
            definition = by_key[key]
            executor = definition.get('executor', 'process')
            if (key not in schemas or executor == 'local' and key not in mounts
                    or executor == 'process' and process_root is None):
                raise PluginError('CONTRACT_EXECUTOR_MISSING', str(key))
            if executor == 'process' and key in mounts:
                raise PluginError('CONTRACT_EXECUTOR_MISMATCH', str(key))
            for export, contract in definition['provides'].items():
                methods[(manifest['id'], export)] = registry.methods(contract)
            for declaration in definition['requires'].values():
                registry.methods(declaration['contract'])
            validator = schema_validator(schemas[key])
            error = next(validator.iter_errors(manifest['config']['configuration']), None)
            if error is not None:
                raise PluginError('INVALID_PLUGIN_CONFIG', error.message)
            mount = mounts.get(key)
            if mount is None:
                root = Path(process_root).resolve()
                mount = process_mount(definition, registry, cwd=root,
                    journal=root / '.plugin-journal' / manifest['id'])
            spec = _spec(definition, manifest, registry, mount)
            specs.append(spec)
            entries.append({'id': manifest['id'], 'use': spec.key, 'config': manifest['config']})
        self.plan = plan
        self._methods = methods
        self._entrypoint = wire(application['entrypoint'])
        self.host = PluginHost({'version': 1, 'plugins': entries}, specs)

    def run(self, request):
        return self.call(self._entrypoint['plugin'], self._entrypoint['export'], 'run', request)

    def call(self, instance, export, method, request, *, timeout=None):
        service = self.plan['exports'].get(instance, {}).get(export)
        if service is None:
            raise PluginError('CONTRACT_EXPORT_UNKNOWN', f'{instance}.{export}')
        if method not in self._methods[(instance, export)]:
            raise PluginError('CONTRACT_METHOD_UNKNOWN', method)
        return self.host.bind(service).invoke(method, args=(wire(request),), timeout=timeout)

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
