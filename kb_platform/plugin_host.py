from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock, current_thread
from types import MappingProxyType
import warnings

from .plugin_native import PluginError, native_call


_leases = ContextVar('plugin_scope_leases', default=())


@dataclass
class _Lease:
    identity: int
    thread: object
    users: int = 1


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

    def __post_init__(self):
        object.__setattr__(self, 'provides', tuple(self.provides))
        object.__setattr__(self, 'requires', tuple(self.requires))


class PluginContext:
    def __init__(self, host, plugin_id, spec):
        self.host, self.plugin_id, self.spec = host, plugin_id, spec
        self._services, self._cleanup, self._listeners = {}, [], []
        self._sealed = False

    def _registration(self):
        if self._sealed:
            raise PluginError('PLUGIN_REGISTRATION_CLOSED', self.plugin_id)

    def require(self, service):
        if service not in self.spec.requires:
            raise PluginError('UNDECLARED_SERVICE_DEPENDENCY', service)
        return self.host.service(service)

    def provide(self, name, implementation):
        with self.host._lock:
            self._registration()
            if name not in self.spec.provides or name in self._services:
                raise PluginError('INVALID_SERVICE_REGISTRATION', name)
            self._services[name] = implementation

    def defer(self, dispose):
        with self.host._lock:
            self._registration()
            if not callable(dispose):
                raise PluginError('INVALID_PLUGIN_DISPOSER')
            self._cleanup.append(dispose)

    def on(self, event: EventName, listener):
        with self.host._lock:
            self._registration()
            event = EventName(event)
            if not callable(listener):
                raise PluginError('INVALID_PLUGIN_LISTENER')
            self._listeners.append((event, listener))

    def _dispose(self):
        with self.host._lock:
            self._sealed = True
        while True:
            with self.host._lock:
                if not self._cleanup:
                    self._listeners.clear()
                    self._services.clear()
                    return
                dispose = self._cleanup[-1]
            try:
                dispose()
            except BaseException as exc:
                raise BaseExceptionGroup('Plugin resource cleanup failed', [exc]) from exc
            with self.host._lock:
                self._cleanup.pop()


class PluginHost:


    def __init__(self, profile: dict, catalog: Iterable[PluginSpec], *, parent=None):
        if (not isinstance(profile, dict) or set(profile) - {'version', 'plugins'}
                or type(profile.get('version')) is not int or profile['version'] != 1):
            raise PluginError('INVALID_PLUGIN_PROFILE')
        self.parent, self._contexts, self._children = parent, {}, {}
        self._lock = parent._lock if parent is not None else RLock()
        self._closed, self._closing, self._phase = False, False, 'starting'
        specs = list(catalog)
        self.catalog = MappingProxyType({spec.key: spec for spec in specs})
        if len(self.catalog) != len(specs):
            raise PluginError('DUPLICATE_PLUGIN_IMPLEMENTATION')
        manifests, self._entries = [], {}
        if not isinstance(profile.get('plugins'), list):
            raise PluginError('INVALID_PLUGIN_PROFILE')
        for entry in deepcopy(profile['plugins']):
            if not isinstance(entry, dict) or set(entry) - {'id', 'use', 'config', 'overrides', 'disabled'}:
                raise PluginError('INVALID_PLUGIN_ENTRY')
            if type(entry.get('disabled', False)) is not bool:
                raise PluginError('INVALID_PLUGIN_ENTRY')
            if entry.get('disabled'):
                continue
            if not isinstance(entry.get('use'), str) or not isinstance(entry.get('id'), str):
                raise PluginError('INVALID_PLUGIN_ENTRY')
            spec = self.catalog.get(entry.get('use'))
            if spec is None:
                raise PluginError('UNKNOWN_PLUGIN_IMPLEMENTATION', str(entry.get('use')))
            plugin_id = entry.get('id')
            if plugin_id in self._entries:
                raise PluginError('DUPLICATE_PLUGIN_ID')
            self._entries[plugin_id] = (spec, entry.get('config', {}))
            manifests.append({'id': plugin_id, 'version': spec.version,
                'provides': list(spec.provides), 'requires': list(spec.requires),
                'config': entry.get('config', {}), 'overrides': entry.get('overrides', [])})
        arguments = {'plugins': manifests}
        if parent is not None:
            arguments['parent'] = parent.id
        with self._lock:
            created = native_call('create', **arguments)
            self.id, self.order = created['host'], tuple(created['order'])
            if parent is not None:
                parent._children[self.id] = self
        try:
            for plugin_id in self.order:
                spec, config = self._entries[plugin_id]
                context = PluginContext(self, plugin_id, spec)
                with self._lock:
                    self._contexts[plugin_id] = context
                spec.mount(context, deepcopy(config))
                with self._lock:
                    if set(context._services) != set(spec.provides):
                        raise PluginError('PLUGIN_SERVICES_INCOMPLETE', plugin_id)
                    context._sealed = True
                    native_call('activate', host=self.id, plugin=plugin_id)
            with self._lock:
                native_call('ready', host=self.id)
                self._phase = 'ready'
        except BaseException as failure:
            with self._lock:
                native_call('abort', host=self.id)
                self._phase, self._closing = 'disposing', True
            try:
                self._cleanup()
            except BaseException as cleanup_error:
                failure.add_note(f'Cleanup failed; retained host {self.id}: {cleanup_error}')
                failure.failed_host = self
            finally:
                with self._lock:
                    self._closing = False
            raise

    def service(self, name):

        with self._lock:
            owner = native_call('resolve', host=self.id, service=name)
            scope = self
            while scope is not None and scope.id != owner['host']:
                scope = scope.parent
            if scope is None:
                raise PluginError('INVALID_SERVICE_SCOPE')
            context = scope._contexts.get(owner['plugin'])
            if context is None or name not in context._services:
                raise PluginError('PLUGIN_SERVICE_DISPOSED', name)
            return context._services[name]

    @contextmanager
    def lease(self):
        with self._lock:
            authority = next((lease for host, lease in _leases.get() if host == self.id), None)


            if authority is not None and authority.users and authority.thread is current_thread():
                authority.users += 1
            else:
                authority = _Lease(native_call('acquire', host=self.id)['lease'], current_thread())
            token = _leases.set(tuple(item for item in _leases.get() if item[0] != self.id)
                                + ((self.id, authority),))
        try:
            yield self
        finally:
            _leases.reset(token)
            with self._lock:
                authority.users -= 1
                if not authority.users:
                    native_call('release', host=self.id, lease=authority.identity)

    def call(self, service, method, *args, **kwargs):
        with self.lease():
            return getattr(self.service(service), method)(*args, **kwargs)

    def emit(self, event: EventName, payload):
        event = EventName(event)


        with self.lease():
            frozen = _freeze(deepcopy(payload))
            with self._lock:
                chain, scope, listeners = [], self, []
                while scope is not None:
                    chain.append(scope)
                    scope = scope.parent
                for scope in reversed(chain):
                    for plugin_id in scope.order:
                        context = scope._contexts.get(plugin_id)
                        if context is not None:
                            listeners.extend(listener for name, listener in context._listeners if name == event)
            failures = []
            for listener in listeners:
                try:
                    listener(frozen)
                except Exception as exc:
                    failures.append(exc)
                    try:
                        warnings.warn(f'Plugin observer failed for {event}: {type(exc).__name__}', RuntimeWarning,
                                      stacklevel=2)
                    except Exception:
                        pass
            return tuple(failures)

    def scope(self, profile, catalog=None):
        return PluginHost(profile, self.catalog.values() if catalog is None else catalog, parent=self)

    def drain(self):
        with self._lock:
            if not self._closed and self._phase not in {'draining', 'disposing'}:
                native_call('drain', host=self.id)
                self._phase = 'draining'

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._closing or self._phase == 'starting':
                raise PluginError('PLUGIN_LIFECYCLE_BUSY')
            self._closing = True
        try:
            self.drain()
            with self._lock:
                self._quiescent()
                children = tuple(self._children.values())
            for child in children:
                child.close()
            with self._lock:
                if self._phase != 'disposing':
                    native_call('dispose', host=self.id)
                    self._phase = 'disposing'
            self._cleanup()
        finally:
            with self._lock:
                self._closing = False

    def _quiescent(self):
        pending = [self]
        while pending:
            scope = pending.pop()
            if scope._phase == 'starting' or (scope is not self and scope._closing):
                raise PluginError('PLUGIN_LIFECYCLE_BUSY')
            if native_call('inspect', host=scope.id)['leases']:
                raise PluginError('scope_busy', 'scope subtree still has active leases')
            pending.extend(scope._children.values())

    def _cleanup(self):
        with self._lock:
            contexts = tuple(self._contexts.items())
        for plugin_id, context in reversed(contexts):
            try:
                context._dispose()
            except BaseException as exc:
                raise BaseExceptionGroup('Plugin cleanup did not finish', [exc]) from exc
            with self._lock:
                self._contexts.pop(plugin_id)
        with self._lock:
            native_call('finish_dispose', host=self.id)
            self._closed, self._phase = True, 'closed'
            if self.parent is not None:
                self.parent._children.pop(self.id, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
