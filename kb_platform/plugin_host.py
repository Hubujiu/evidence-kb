from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, RLock, Timer, current_thread
from types import MappingProxyType
from time import monotonic
import warnings

from jsonschema import Draft202012Validator, validators

from .plugin_native import PluginError, native_call


_leases = ContextVar('plugin_scope_leases', default=())
_executing = ContextVar('plugin_lifecycle_action', default=None)
_current_call = ContextVar('plugin_current_call', default=None)
_clock_lock = RLock()
_ConfigValidator = validators.extend(Draft202012Validator, type_checker=
    Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda checker, value: type(value) is int))


def _advance_clock(host):
    with _clock_lock:
        now = int(monotonic() * 1000)
        native_call('runtime_tick', host=host, now=now)
        return now


@dataclass
class _Lease:
    identity: int
    thread: object
    users: int = 1
    operation: int | None = None
    completion: dict | None = None
    parent_operation: int | None = None


@dataclass(frozen=True)
class _ActionFailure:
    host: int
    operation: int
    error: BaseException


class EventName(StrEnum):
    RUN_STARTED = 'run.started'
    RUN_COMPLETED = 'run.completed'
    RUN_FAILED = 'run.failed'
    MODEL_BEFORE = 'model.before'
    MODEL_AFTER = 'model.after'
    TOOL_BEFORE = 'tool.before'
    TOOL_AFTER = 'tool.after'


def _freeze(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise PluginError('INVALID_EVENT_METADATA')
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise PluginError('INVALID_EVENT_METADATA')


@dataclass(frozen=True)
class PluginSpec:
    key: str
    provides: tuple[str, ...]
    requires: tuple[str, ...]
    mount: Callable
    version: str = '1'
    concurrency: str = 'concurrent'
    config_schema: dict | None = None
    config_check: Callable | None = None
    config_inputs: Callable | None = None

    def __post_init__(self):
        object.__setattr__(self, 'provides', tuple(self.provides))
        object.__setattr__(self, 'requires', tuple(self.requires))
        if self.config_schema is not None:
            Draft202012Validator.check_schema(self.config_schema)
            object.__setattr__(self, 'config_schema', deepcopy(self.config_schema))


class PluginContext:
    def __init__(self, host, action, spec):
        self.host, self.plugin_id, self.spec = host, action['plugin'], spec
        self.identity = MappingProxyType({key: action[key] for key in
            ('runtime', 'host', 'instance', 'generation', 'operation')})
        self._services, self._resources, self._listeners = {}, {}, {}
        self._bindings = action['bindings']
        self._sealed, self._cleanup_operation = False, None

    def _registration(self):
        if self._sealed:
            raise PluginError('PLUGIN_REGISTRATION_CLOSED', self.plugin_id)

    def require(self, service):
        return self.host._implementation(self.binding(service))

    def binding(self, service):
        if service not in self.spec.requires:
            raise PluginError('UNDECLARED_SERVICE_DEPENDENCY', service)
        binding = native_call('runtime_resolve', host=self.host.id, service=service,
            consumer=self.identity['instance'], consumer_generation=self.identity['generation'],
            cleanup=self._cleanup_operation)
        expected = self._bindings.get(service)
        if expected is None or expected['registration'] != binding['registration']:
            raise PluginError('stale_binding', service)
        return MappingProxyType(binding)

    def provide(self, name, implementation):
        with self.host._lock:
            self._registration()
            if name not in self.spec.provides or name in self._services:
                raise PluginError('INVALID_SERVICE_REGISTRATION', name)
            self._services[name] = implementation

    def registration_id(self, kind='resource'):
        with self.host._lock:
            self._registration()
            resource = native_call('runtime_resource', host=self.host.id,
                operation=self.identity['operation'], kind=kind)['resource']
            self._resources[resource] = None
            return resource

    def defer(self, dispose):
        if not callable(dispose):
            raise PluginError('INVALID_PLUGIN_DISPOSER')
        with self.host._lock:
            resource = self.registration_id('disposer')
            self._resources[resource] = dispose
        return resource

    def effect(self, dispose):
        call = self.host.current_call
        if (call is None or call['instance'] != self.identity['instance']
                or call['generation'] != self.identity['generation'] or not callable(dispose)):
            raise PluginError('INVALID_PLUGIN_EFFECT_CONTEXT')
        with self.host._lock:
            resource = native_call('runtime_resource', host=call['host'],
                operation=call['operation'], kind='effect')['resource']
            self._resources[resource] = dispose
        return resource

    def on(self, event, listener, *, priority=0):
        if not callable(listener):
            raise PluginError('INVALID_PLUGIN_LISTENER')
        with self.host._lock:
            self._registration()
            resource = native_call('runtime_listener', host=self.host.id,
                operation=self.identity['operation'], event=str(event), priority=priority)['resource']
            self._resources[resource] = None
            self._listeners[resource] = listener
        return resource

    @contextmanager
    def lease(self, *, parent_lease=None):
        authority = self.host._authority()
        call_authority = self.host._call_authority(self.identity)
        if call_authority is not None and (authority is None
                or (authority.operation or authority.parent_operation) != call_authority.operation):
            authority = call_authority
        acquired = native_call('runtime_acquire', host=self.host.id, instance=self.identity['instance'],
            generation=self.identity['generation'],
            parent_lease=parent_lease if parent_lease is not None else authority.identity if authority else None)
        lease = _Lease(acquired['lease'], current_thread(),
            parent_operation=(authority.operation or authority.parent_operation) if authority else None)
        with self.host._lock:
            token = _leases.set(tuple(item for item in _leases.get() if item[0] != self.host.id)
                + ((self.host.id, lease),))
        try:
            yield self
        finally:
            _leases.reset(token)
            self.host._release(lease)


class ServiceBinding:
    def __init__(self, host, binding):
        self.host, self.identity = host, MappingProxyType(binding)

    def call(self, method, *args, **kwargs):
        return self.host._invoke(dict(self.identity), method, args, kwargs)

    def invoke(self, method, *, args=(), kwargs=None, timeout=None):
        return self.host._invoke(dict(self.identity), method, args, kwargs or {}, timeout=timeout)


class PluginHost:
    def __init__(self, profile: dict, catalog: Iterable[PluginSpec], *, parent=None, dynamic=False,
                 isolated=(), intercept=None):
        self.parent = parent
        self.dynamic = dynamic
        self._lock = parent._lock if parent is not None else RLock()
        self._root = parent._root if parent is not None else self
        if parent is None:
            self._pump_owner = None
        self._cancellations = parent._cancellations if parent is not None else {}
        self._contexts, self._children = {}, {}
        self._final_snapshot = None
        self._collection = None
        self.isolated, self.intercept = tuple(isolated), deepcopy(intercept or {})
        specs = list(catalog)
        self.catalog = MappingProxyType({spec.key: spec for spec in specs})
        if len(self.catalog) != len(specs):
            raise PluginError('DUPLICATE_PLUGIN_IMPLEMENTATION')
        manifests, entries = self._prepare(profile)
        arguments = {'plugins': manifests, 'parent': parent.id if parent else None,
            'isolated': list(self.isolated), 'intercept': self.intercept}
        if not dynamic:
            native_call('runtime_validate', **arguments)
        self._entries = entries
        pump_owner = object()
        with self._lock:
            if self._root._pump_owner is not None:
                raise PluginError('PLUGIN_LIFECYCLE_BUSY')
            self._root._pump_owner = pump_owner
            try:
                created = native_call('runtime_create', **arguments)
                self.id, self.runtime_id, self.order = created['host'], created['runtime'], tuple(created['order'])
                if parent is not None:
                    parent._children[self.id] = self
            except BaseException:
                self._root._pump_owner = None
                raise
        failures = [failure.error for failure in self._pump(owner=pump_owner) if failure.host == self.id]
        if failures:
            failure = failures[0]
            native_call('runtime_drain', host=self.id, remove=True)
            cleanup_failures = self._pump()
            if self.inspect()['state'] != 'disposed':
                failure.failed_host = self
                failure.add_note(f'Cleanup retained host {self.id}: {cleanup_failures}')
            else:
                self._collect()
            raise failure
        if self.inspect()['state'] == 'disposed':
            raise PluginError('scope_unavailable', 'scope stopped during initialization')

    def _prepare(self, profile):
        if (not isinstance(profile, dict) or set(profile) - {'version', 'plugins'}
                or type(profile.get('version')) is not int or profile['version'] != 1
                or not isinstance(profile.get('plugins'), list)):
            raise PluginError('INVALID_PLUGIN_PROFILE')
        manifests, entries, seen = [], {}, set()
        for entry in deepcopy(profile['plugins']):
            if (not isinstance(entry, dict) or set(entry) - {'id', 'use', 'config', 'overrides', 'disabled'}
                    or type(entry.get('disabled', False)) is not bool
                    or not isinstance(entry.get('id'), str) or not isinstance(entry.get('use'), str)
                    or not isinstance(entry.get('config', {}), dict)):
                raise PluginError('INVALID_PLUGIN_ENTRY')
            plugin_id = entry['id']
            if plugin_id in seen:
                raise PluginError('DUPLICATE_PLUGIN_ID')
            seen.add(plugin_id)
            spec = self.catalog.get(entry['use'])
            if spec is None:
                raise PluginError('UNKNOWN_PLUGIN_IMPLEMENTATION', entry['use'])
            self._validate_config(spec, entry.get('config', {}))
            if entry.get('disabled'):
                continue
            entries[plugin_id] = (spec, entry.get('config', {}))
            manifests.append({'id': plugin_id, 'version': spec.version, 'provides': list(spec.provides),
                'requires': list(spec.requires), 'config': entry.get('config', {}),
                'overrides': entry.get('overrides', []), 'concurrency': spec.concurrency,
                'implementation': spec.key, 'configuration_inputs': self._configuration_inputs(spec,
                    entry.get('config', {}))})
        return manifests, entries

    @staticmethod
    def _validate_config(spec, config):
        if spec.config_schema is not None:
            failures = list(_ConfigValidator(spec.config_schema).iter_errors(config))
            if failures:
                raise PluginError('INVALID_PLUGIN_CONFIG', f'{spec.key}: {failures[0].message}')
        if spec.config_check is not None:
            spec.config_check(deepcopy(config))

    @staticmethod
    def _configuration_inputs(spec, config):
        return spec.config_inputs(deepcopy(config)) if spec.config_inputs is not None else {}

    def _find_scope(self, identity):
        scopes = [self._root]
        while scopes:
            scope = scopes.pop()
            if scope.id == identity:
                return scope
            scopes.extend(scope._children.values())
        raise PluginError('INVALID_SERVICE_SCOPE')

    def _implementation(self, binding):
        with self._lock:
            scope = self._find_scope(binding['host'])
            context = scope._contexts.get((binding['instance'], binding['generation']))
            if context is None or binding['service'] not in context._services:
                raise PluginError('PLUGIN_SERVICE_DISPOSED', binding['service'])
            return context._services[binding['service']]

    def _execute(self, action):
        scope = self._find_scope(action['host'])
        key = action['instance'], action['generation']
        if action['kind'] == 'init':
            spec = scope.catalog[action['implementation']]
            context = PluginContext(scope, action, spec)
            with self._lock:
                scope._contexts[key] = context
            try:
                if self._configuration_inputs(spec, action['config']) != action['configuration_inputs']:
                    raise PluginError('CONFIGURATION_INPUT_CHANGED', action['plugin'])
                spec.mount(context, deepcopy(action['config']))
                if set(context._services) != set(spec.provides):
                    raise PluginError('PLUGIN_SERVICES_INCOMPLETE', action['plugin'])
            finally:
                context._sealed = True
            return list(context._services)
        if action['kind'] == 'cleanup':
            with self._lock:
                context = scope._contexts.get(key)
            if context is None:
                return []
            resource = action['resource']
            context._cleanup_operation = action['operation']
            try:
                if resource == 0:
                    with self._lock:
                        scope._contexts.pop(key, None)
                else:
                    callback = context._resources.get(resource)
                    if callback is not None:
                        callback()
                    with self._lock:
                        context._resources.pop(resource, None)
                        context._listeners.pop(resource, None)
            finally:
                context._cleanup_operation = None
            return []
        raise PluginError('UNKNOWN_PLUGIN_ACTION', action['kind'])

    def _pump(self, *, owner=None):
        root = self._root
        with self._lock:
            if owner is None:
                if root._pump_owner is not None or root._final_snapshot is not None:
                    return []
                owner = object()
                root._pump_owner = owner
            elif root._pump_owner is not owner:
                raise PluginError('PLUGIN_LIFECYCLE_BUSY')
        failures = []
        try:
            while True:
                with self._lock:
                    if root._final_snapshot is not None:
                        root._pump_owner = None
                        return failures
                    actions = native_call('runtime_poll', host=root.id, limit=1)['actions']
                    if not actions:
                        root._pump_owner = None
                        return failures
                action = actions[0]
                if action['kind'] == 'cancel':
                    with self._lock:
                        signal = self._cancellations.get(action['operation'])
                    if signal is not None:
                        signal.set()
                    continue
                token = _executing.set((self.runtime_id, action['operation']))
                try:
                    services = self._execute(action)
                except BaseException as exc:
                    completion = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
                    failures.append(_ActionFailure(action['host'], action['operation'], exc))
                else:
                    completion = {'status': 'ok', 'services': services}
                finally:
                    _executing.reset(token)
                with self._lock:
                    native_call('runtime_complete', host=action['host'], operation=action['operation'],
                        **completion)
                    native_call('runtime_ack', host=action['host'], operations=[action['operation']])
        finally:
            with self._lock:
                if root._pump_owner is owner:
                    root._pump_owner = None

    def inspect(self):
        if self._final_snapshot is not None:
            return deepcopy(self._final_snapshot)
        return native_call('runtime_inspect', host=self.id)

    def bind(self, name):
        authority = self._authority()
        return ServiceBinding(self, native_call('runtime_resolve', host=self.id, service=name,
            parent_lease=authority.identity if authority else None))

    def service(self, name):
        return self._implementation(native_call('runtime_resolve', host=self.id, service=name))

    def _authority(self):
        return next((lease for host, lease in _leases.get() if host == self.id
            and lease.users and lease.thread is current_thread()), None)

    def _call_authority(self, identity):
        call = self.current_call
        if (call is None or call['runtime'] != identity['runtime']
                or call['instance'] != identity['instance'] or call['generation'] != identity['generation']):
            return None
        return next((lease for host, lease in _leases.get() if host == call['host']
            and lease.identity == call['lease'] and lease.operation == call['operation']
            and lease.users and lease.thread is current_thread()), None)

    def _settle(self, authority):
        if authority.operation is None:
            native_call('runtime_release', host=self.id, lease=authority.identity)
        else:
            native_call('runtime_complete', host=self.id, operation=authority.operation,
                **authority.completion)
            native_call('runtime_ack', host=self.id, operations=[authority.operation])

    def _release(self, authority):
        with self._lock:
            authority.users -= 1
            settled = not authority.users
            if settled:
                self._settle(authority)
        if settled:
            self._pump()

    @contextmanager
    def lease(self):
        with self._lock:
            authority = self._authority()
            if authority is not None:
                authority.users += 1
            else:
                authority = _Lease(native_call('runtime_acquire', host=self.id)['lease'], current_thread())
            token = _leases.set(tuple(item for item in _leases.get() if item[0] != self.id)
                + ((self.id, authority),))
        try:
            yield self
        finally:
            _leases.reset(token)
            self._release(authority)

    def _invoke(self, binding, method, args, kwargs, *, timeout=None):
        deadline = None
        if timeout is not None:
            if type(timeout) not in {int, float} or not 0 < timeout <= 86400:
                raise PluginError('INVALID_CALL_TIMEOUT')
            now = _advance_clock(self.id)
            deadline = now + max(1, int(timeout * 1000))
        invocation = _Lease(0, current_thread())
        action = None
        timer = None
        timer_started = False
        call_token = token = None
        try:
            with self._lock:
                authority = self._authority()
                action = native_call('runtime_invoke', host=self.id, registration=binding['registration'],
                    method=method, parent_lease=authority.identity if authority else None, deadline=deadline)
                invocation.identity, invocation.operation = action['lease'], action['operation']
                deadline = action['deadline']
                signal = Event()
                self._cancellations[action['operation']] = signal
            if deadline is not None:
                def expire():
                    with self._lock:
                        if action['operation'] not in self._cancellations:
                            return
                        _advance_clock(self.id)
                        result = native_call('runtime_cancel', host=self.id, operation=action['operation'])
                        if result['cancelled']:
                            signal.set()
                timer = Timer(max(0, deadline / 1000 - monotonic()), expire)
                timer.daemon = True
                timer.start()
                timer_started = True
            call_token = _current_call.set(MappingProxyType({**action, 'cancel_signal': signal}))
            token = _leases.set(tuple(item for item in _leases.get() if item[0] != self.id)
                + ((self.id, invocation),))
            result = getattr(self._implementation(binding), method)(*args, **kwargs)
        except BaseException as exc:
            invocation.completion = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
            raise
        else:
            invocation.completion = {'status': 'ok'}
            return result
        finally:
            if action is not None:
                try:
                    if timer is not None:
                        timer.cancel()
                        if timer_started:
                            timer.join()
                    if call_token is not None:
                        _current_call.reset(call_token)
                    if token is not None:
                        _leases.reset(token)
                finally:
                    with self._lock:
                        self._cancellations.pop(action['operation'], None)
                    self._release(invocation)

    def call(self, service, method, *args, **kwargs):
        return self.bind(service).call(method, *args, **kwargs)

    @property
    def current_call(self):
        return _current_call.get()

    def cancel(self, operation):
        with self._lock:
            result = native_call('runtime_cancel', host=self.id, operation=operation)
            signal = self._cancellations.get(operation)
            if result['cancelled'] and signal is not None:
                signal.set()
        self._raise_failures(self._pump(), 'Cancellation effects failed')
        return result

    def dispatch(self, event, payload, *, mode='emit'):
        if mode not in {'emit', 'parallel', 'serial', 'bail', 'waterfall'}:
            raise PluginError('INVALID_EVENT_MODE')
        with self.lease():
            event_lease = self._authority().identity
            rows = native_call('runtime_listeners', host=self.id, event=str(event))['listeners']
            with self._lock:
                listeners = []
                for row in rows:
                    scope = self._find_scope(row['host'])
                    context = scope._contexts[(row['instance'], row['generation'])]
                    listeners.append((context, context._listeners[row['resource']]))
            value = _freeze(deepcopy(payload)) if mode in {'emit', 'parallel', 'serial', 'bail'} else payload
            def run(item, argument):
                context, callback = item
                with context.lease(parent_lease=event_lease):
                    try:
                        return callback(argument)
                    except BaseException as exc:
                        native_call('runtime_diagnostic', host=context.host.id, code='listener_failed',
                            message=f'{event}/{mode}: {type(exc).__name__}', instance=context.identity['instance'])
                        raise
            if mode == 'parallel':
                with ThreadPoolExecutor(max_workers=max(1, min(32, len(listeners)))) as pool:
                    futures = [pool.submit(run, item, value) for item in listeners]
                    results, failures = [], []
                    for future in futures:
                        try:
                            results.append(future.result())
                        except BaseException as exc:
                            failures.append(exc)
                    if failures:
                        raise BaseExceptionGroup('Parallel plugin listeners failed', failures)
                    return tuple(results)
            if mode == 'waterfall':
                def invoke(index, argument):
                    if index == len(listeners):
                        return argument
                    used = False
                    def next_(next_value=argument):
                        nonlocal used
                        if used:
                            raise PluginError('WATERFALL_NEXT_REUSED')
                        used = True
                        return invoke(index + 1, next_value)
                    context, callback = listeners[index]
                    with context.lease(parent_lease=event_lease):
                        return callback(argument, next_)
                return invoke(0, value)
            failures = []
            for item in listeners:
                try:
                    result = run(item, value)
                except Exception as exc:
                    if mode != 'emit':
                        raise
                    failures.append(exc)
                    try:
                        warnings.warn(f'Plugin observer failed for {event}: {type(exc).__name__}',
                            RuntimeWarning, stacklevel=2)
                    except Exception:
                        pass
                else:
                    if mode in {'bail', 'serial'} and result is not None and result is not False:
                        return result
            if mode == 'emit':
                return tuple(failures)
            return None

    def emit(self, event, payload):
        return self.dispatch(event, payload)

    def scope(self, profile, catalog=None, *, dynamic=False, isolated=(), intercept=None):
        return PluginHost(profile, self.catalog.values() if catalog is None else catalog,
            parent=self, dynamic=dynamic, isolated=isolated, intercept=intercept)

    def _lifecycle_allowed(self):
        action = _executing.get()
        if action is not None and action[0] == self.runtime_id:
            raise PluginError('PLUGIN_LIFECYCLE_BUSY')

    def drain(self):
        self._lifecycle_allowed()
        if self.inspect()['state'] != 'disposed':
            native_call('runtime_drain', host=self.id, remove=False)

    def apply(self, profile):
        self._lifecycle_allowed()
        manifests, entries = self._prepare(profile)
        if not self.dynamic:
            native_call('runtime_validate', plugins=manifests, parent=self.parent.id if self.parent else None,
                isolated=list(self.isolated), intercept=self.intercept)
        with self._lock:
            result = native_call('runtime_apply', host=self.id, plugins=manifests)
            self._entries = entries
        self._raise_failures(self._pump(), 'Plugin configuration did not finish')
        return {**result, 'runtime': self.inspect()}

    def update(self, plugin, *, enabled=None, config=None, remove=False):
        self._lifecycle_allowed()
        selected_config = deepcopy(config)
        while True:
            with self._lock:
                entry = self._entries.get(plugin)
                if entry is None:
                    raise PluginError('unknown_plugin', plugin)
            spec, prior_config = entry
            configuration_inputs = None
            if config is not None or not (remove or enabled is False):
                candidate = selected_config if config is not None else prior_config
                self._validate_config(spec, candidate)
                configuration_inputs = self._configuration_inputs(spec, candidate)
            with self._lock:
                if self._entries.get(plugin) is not entry:
                    continue
                result = native_call('runtime_update', host=self.id, plugin=plugin,
                    enabled=enabled, config=selected_config, remove=remove,
                    configuration_inputs=configuration_inputs)
                if config is not None:
                    self._entries[plugin] = spec, selected_config
                break
        self._raise_failures(self._pump(), 'Plugin update did not finish')
        return {**result, 'runtime': self.inspect()}

    def retry(self, plugin=None):
        self._lifecycle_allowed()
        result = native_call('runtime_retry', host=self.id, plugin=plugin)
        self._raise_failures(self._pump(), 'Plugin retry did not finish')
        return {**result, 'runtime': self.inspect()}

    def _raise_failures(self, failures, message):
        failures = [failure.error for failure in failures if failure.host == self.id]
        if failures:
            failure = BaseExceptionGroup(message, failures)
            failure.failed_host = self
            raise failure

    def _collect(self):
        with self._lock:
            if self._final_snapshot is None:
                for child in tuple(self._children.values()):
                    child._collect()
                self._collection = native_call('runtime_collect', host=self.id)
                self._final_snapshot = self._collection['snapshot']
            if self.parent is not None:
                self.parent._children.pop(self.id, None)

    def close(self):
        self._lifecycle_allowed()
        if self.inspect()['state'] == 'disposed':
            self._collect()
            return
        native_call('runtime_drain', host=self.id, remove=True)
        snapshot = self.inspect()
        if snapshot.get('leases'):
            raise PluginError('scope_busy', 'scope subtree still has active leases')
        if any(not context._sealed for scope in [self, *self._children.values()]
                for context in scope._contexts.values()):
            raise PluginError('PLUGIN_LIFECYCLE_BUSY')
        for child in tuple(self._children.values()):
            child.close()
        native_call('runtime_retry', host=self.id)
        self._raise_failures(self._pump(), 'Plugin cleanup did not finish')
        if self.inspect()['state'] != 'disposed':
            raise PluginError('scope_busy', 'runtime retains unfinished operations or resources')
        self._collect()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
