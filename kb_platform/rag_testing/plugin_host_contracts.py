from contextvars import Context, copy_context
from threading import Event, Thread
import unittest
import warnings

from kb_platform.plugin_host import EventName, PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError, native_call


def profile(*entries):
    return {'version': 1, 'plugins': list(entries)}


def entry(name, **fields):
    return {'id': name, 'use': name, **fields}


def service_spec(name, value):
    return PluginSpec(name, (name,), (), lambda context, config: context.provide(name, value))


def lease_once(host):
    with host.lease():
        return True


class PluginHostContracts(unittest.TestCase):
    def test_dependency_plan_mounts_and_disposes_in_dependency_order(self):
        calls = []
        def provider(context, config):
            calls.append('provider mounted')
            context.provide('model', config['value'])
            context.defer(lambda: calls.append('provider disposed'))
        def consumer(context, config):
            self.assertEqual(context.require('model'), 7)
            calls.append('consumer mounted')
            context.provide('run', object())
            context.defer(lambda: calls.append(f'consumer disposed with {context.require("model")}'))
        catalog = (PluginSpec('consumer', ('run',), ('model',), consumer),
                   PluginSpec('provider', ('model',), (), provider))
        settings = profile(entry('consumer'), entry('provider', config={'value': 7}))
        with PluginHost(settings, catalog) as host:
            settings['plugins'][1]['config']['value'] = 99
            self.assertEqual(host.service('model'), 7)
            self.assertEqual(host.order, ('provider', 'consumer'))
        self.assertEqual(calls, ['provider mounted', 'consumer mounted',
                                'consumer disposed with 7', 'provider disposed'])

    def test_invalid_graphs_fail_before_any_plugin_effect(self):
        calls = []
        def mount(context, config):
            calls.append(context.plugin_id)
        graphs = [
            ([PluginSpec('a', ('x',), ('absent',), mount)], 'missing_service'),
            ([PluginSpec('a', ('x',), ('y',), mount), PluginSpec('b', ('y',), ('x',), mount)],
             'dependency_cycle'),
            ([PluginSpec('a', ('x',), (), mount), PluginSpec('b', ('x',), (), mount)],
             'duplicate_service'),
        ]
        for catalog, code in graphs:
            with self.subTest(code=code), self.assertRaises(PluginError) as failure:
                PluginHost(profile(*(entry(spec.key) for spec in catalog)), catalog)
            self.assertEqual(failure.exception.code, code)
        self.assertEqual(calls, [])

    def test_child_service_overrides_are_explicit_and_isolated(self):
        root_spec = service_spec('model', 'root')
        child_spec = service_spec('model', 'child')
        with PluginHost(profile(entry('model')), [root_spec]) as root:
            with self.assertRaises(PluginError) as failure:
                root.scope(profile(entry('model')), [child_spec])
            self.assertEqual(failure.exception.code, 'implicit_override')
            with root.scope(profile()) as inherited:
                self.assertEqual(inherited.service('model'), 'root')
                with root.scope(profile(entry('model', overrides=['model'])), [child_spec]) as child:
                    self.assertEqual(child.service('model'), 'child')
                    self.assertEqual(inherited.service('model'), 'root')
                    self.assertEqual(root.service('model'), 'root')

    def test_successful_mount_seals_services_listeners_and_disposers(self):
        contexts = []
        def mount(context, config):
            contexts.append(context)
            context.provide('model', 1)
        with PluginHost(profile(entry('model')), [PluginSpec('model', ('model',), (), mount)]) as host:
            context = contexts[0]
            for registration in (lambda: context.provide('model', 2), lambda: context.defer(lambda: None),
                                 lambda: context.on(EventName.RUN_STARTED, lambda payload: None)):
                with self.assertRaises(PluginError) as failure:
                    registration()
                self.assertEqual(failure.exception.code, 'PLUGIN_REGISTRATION_CLOSED')
            with self.assertRaises(TypeError):
                host.catalog['new'] = service_spec('new', 2)
            self.assertEqual(host.service('model'), 1)

    def test_observers_receive_immutable_metadata_and_failures_do_not_stop_delivery(self):
        seen = []
        def mount(context, config):
            def bad(payload):
                with self.assertRaises(TypeError):
                    payload['counts']['tools'] = 9
                with self.assertRaises(AttributeError):
                    payload['names'].append('mutated')
                raise RuntimeError('observer failed')
            context.on(EventName.RUN_COMPLETED, bad)
            context.on(EventName.RUN_COMPLETED, lambda payload: seen.append(payload['counts']['tools']))
        with PluginHost(profile(entry('observe')), [PluginSpec('observe', (), (), mount)]) as host:
            payload = {'counts': {'tools': 1}, 'names': ['read']}
            with warnings.catch_warnings(record=True) as messages:
                warnings.simplefilter('always')
                failures = host.emit(EventName.RUN_COMPLETED, payload)
            self.assertEqual(len(failures), 1)
            self.assertEqual(len(messages), 1)
            with warnings.catch_warnings():
                warnings.simplefilter('error')
                self.assertEqual(len(host.emit(EventName.RUN_COMPLETED, payload)), 1)
            self.assertEqual(seen, [1, 1])
            self.assertEqual(payload, {'counts': {'tools': 1}, 'names': ['read']})
            with self.assertRaises(PluginError):
                host.emit(EventName.RUN_COMPLETED, {'arbitrary_mutable_object': object()})

    def test_disposal_retry_preserves_dependencies_and_does_not_repeat_successes(self):
        calls, fail = [], [True]
        def provider(context, config):
            context.provide('model', 'alive')
            context.defer(lambda: calls.append('provider'))
        def consumer(context, config):
            def pending():
                calls.append(f'pending {context.require("model")}')
                if fail[0]:
                    raise RuntimeError('resource busy')
            context.defer(lambda: calls.append('consumer bottom'))
            context.defer(pending)
            context.defer(lambda: calls.append('consumer top'))
        host = PluginHost(profile(entry('provider'), entry('consumer')),
            [PluginSpec('provider', ('model',), (), provider),
             PluginSpec('consumer', (), ('model',), consumer)])
        try:
            with self.assertRaises(ExceptionGroup):
                host.close()
            self.assertEqual(calls, ['consumer top', 'pending alive'])
            self.assertEqual(host.service('model'), 'alive')
            self.assertEqual(native_call('inspect', host=host.id)['state'], 'disposing')
        finally:
            fail[0] = False
            host.close()
        host.close()
        self.assertEqual(calls, ['consumer top', 'pending alive', 'pending alive', 'consumer bottom', 'provider'])

    def test_failed_mount_recovery_retains_parent_until_partial_resources_close(self):
        calls, busy = [], [True]
        root = PluginHost(profile(entry('model')), [service_spec('model', 'parent')])
        def broken(context, config):
            def cleanup():
                calls.append(context.require('model'))
                if busy[0]:
                    raise RuntimeError('cannot release yet')
            context.defer(cleanup)
            raise ValueError('mount failed')
        try:
            with self.assertRaises(ValueError) as failure:
                root.scope(profile(entry('broken')), [PluginSpec('broken', (), ('model',), broken)])
            failed_host = failure.exception.failed_host
            self.assertEqual(native_call('inspect', host=root.id)['children'], [failed_host.id])
            with self.assertRaises(ExceptionGroup):
                root.close()
            self.assertEqual(root.service('model'), 'parent')
        finally:
            busy[0] = False
            root.close()
        self.assertEqual(calls, ['parent', 'parent', 'parent'])

    def test_mount_failure_rolls_back_partial_and_activated_effects(self):
        calls = []
        def provider(context, config):
            context.provide('model', 1)
            context.defer(lambda: calls.append('provider'))
        def broken(context, config):
            context.defer(lambda: calls.append('partial'))
            raise ValueError('stop')
        with PluginHost(profile(), []) as parent:
            with self.assertRaises(ValueError) as failure:
                parent.scope(profile(entry('provider'), entry('broken')),
                    [PluginSpec('provider', ('model',), (), provider),
                     PluginSpec('broken', (), ('model',), broken)])
            self.assertFalse(hasattr(failure.exception, 'failed_host'))
            self.assertEqual(native_call('inspect', host=parent.id)['children'], [])
        self.assertEqual(calls, ['partial', 'provider'])

    def test_draining_waits_for_lease_and_rejects_new_work(self):
        disposed = []
        def mount(context, config):
            context.provide('value', 1)
            context.defer(lambda: disposed.append(True))
        host = PluginHost(profile(entry('value')), [PluginSpec('value', ('value',), (), mount)])
        try:
            with host.lease():
                with self.assertRaises(PluginError) as failure:
                    host.close()
                self.assertEqual(failure.exception.code, 'scope_busy')
                self.assertEqual(disposed, [])
                self.assertEqual(host.service('value'), 1)
                with self.assertRaises(PluginError) as failure:
                    Context().run(lease_once, host)
                self.assertEqual(failure.exception.code, 'scope_unavailable')
        finally:
            host.close()
        self.assertEqual(disposed, [True])

    def test_ancestor_drain_quiesces_child_without_revoking_its_live_lease(self):
        root = PluginHost(profile(), [])
        child = root.scope(profile())
        try:
            with child.lease():
                root.drain()
                with self.assertRaises(PluginError):
                    Context().run(lease_once, child)
                with self.assertRaises(PluginError):
                    root.close()
        finally:
            root.close()

    def test_subtree_is_quiescent_before_any_child_resource_is_disposed(self):
        for lease_target in ('parent', 'second-child'):
            with self.subTest(lease_target=lease_target):
                disposed = []
                def resource(context, config):
                    context.provide('value', config['value'])
                    context.defer(lambda: disposed.append(config['value']))
                spec = PluginSpec('resource', ('value',), (), resource)
                parent = PluginHost(profile(), [])
                first = parent.scope(profile(entry('resource', config={'value': 'first'})), [spec])
                second = parent.scope(profile(entry('resource', config={'value': 'second'})), [spec])
                owner = parent if lease_target == 'parent' else second
                try:
                    with owner.lease():
                        with self.assertRaises(PluginError) as failure:
                            parent.close()
                        self.assertEqual(failure.exception.code, 'scope_busy')
                        self.assertEqual(disposed, [])
                        self.assertEqual(first.service('value'), 'first')
                        self.assertEqual(second.service('value'), 'second')
                finally:
                    parent.close()
                self.assertEqual(disposed, ['first', 'second'])

    def test_admitted_nested_calls_share_lease_after_drain_but_new_threads_do_not(self):
        failures = []
        class Action:
            def invoke(self):
                return 'completed'
        host = PluginHost(profile(entry('action')), [service_spec('action', Action())])
        def new_request():
            try:
                host.call('action', 'invoke')
            except PluginError as exc:
                failures.append(exc.code)
        try:
            with host.lease():
                host.drain()
                self.assertEqual(host.call('action', 'invoke'), 'completed')
                self.assertEqual(native_call('inspect', host=host.id)['leases'], 1)
                worker = Thread(target=new_request)
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, ['scope_unavailable'])
        finally:
            host.close()

    def test_inherited_lease_lives_until_last_user_and_stale_context_cannot_revive_it(self):
        host = PluginHost(profile(), [])
        try:
            with host.lease():
                inherited = copy_context()
                nested = host.lease()
                inherited.run(nested.__enter__)
                host.drain()
            self.assertEqual(native_call('inspect', host=host.id)['leases'], 1)
            with self.assertRaises(PluginError):
                host.close()
            inherited.run(nested.__exit__, None, None, None)
            self.assertEqual(native_call('inspect', host=host.id)['leases'], 0)
            with self.assertRaises(PluginError) as failure:
                inherited.run(lease_once, host)
            self.assertEqual(failure.exception.code, 'scope_unavailable')
        finally:
            host.close()

    def test_base_exceptions_during_cleanup_are_retained_and_retryable(self):
        interrupted = [True]
        def mount(context, config):
            def cleanup():
                if interrupted[0]:
                    raise KeyboardInterrupt('synthetic interrupt')
            context.defer(cleanup)
        host = PluginHost(profile(entry('resource')), [PluginSpec('resource', (), (), mount)])
        try:
            with self.assertRaises(BaseExceptionGroup) as failure:
                host.close()
            self.assertNotIsInstance(failure.exception, ExceptionGroup)
        finally:
            interrupted[0] = False
            host.close()

    def test_parent_close_during_child_mount_cannot_dispose_live_child_resources(self):
        entered, release = Event(), Event()
        failures, calls = [], []
        parent = PluginHost(profile(), [])
        def mount(context, config):
            context.defer(lambda: calls.append('released'))
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test synchronization failed')
        def create_child():
            try:
                parent.scope(profile(entry('slow')), [PluginSpec('slow', (), (), mount)])
            except BaseException as exc:
                failures.append(exc)
        worker = Thread(target=create_child)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(PluginError) as failure:
                parent.close()
            self.assertEqual(failure.exception.code, 'PLUGIN_LIFECYCLE_BUSY')
            self.assertEqual(calls, [])
        finally:
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            parent.close()
        self.assertEqual(calls, ['released'])
        self.assertEqual([failure.code for failure in failures], ['scope_unavailable'])

    def test_cleanup_callbacks_run_without_holding_scope_metadata_lock(self):
        observed = []
        def mount(context, config):
            context.provide('value', 8)
            def cleanup():
                worker = Thread(target=lambda: observed.append(context.host.service('value')))
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive(), 'resource callback held scope metadata lock')
            context.defer(cleanup)
        with PluginHost(profile(entry('value')), [PluginSpec('value', ('value',), (), mount)]):
            pass
        self.assertEqual(observed, [8])

    def test_observer_lease_keeps_parent_resources_alive_until_child_emit_finishes(self):
        entered, release = Event(), Event()
        disposed, results, failures = [], [], []

        def mount(context, config):
            context.defer(lambda: disposed.append('parent'))

            def listener(payload):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test synchronization failed')
                results.append(tuple(disposed))

            context.on(EventName.RUN_STARTED, listener)

        root = PluginHost(profile(entry('observer')), [PluginSpec('observer', (), (), mount)])
        child = root.scope(profile())

        def emit():
            try:
                failures.extend(child.emit(EventName.RUN_STARTED, {}))
            except BaseException as exc:
                failures.append(exc)

        worker = Thread(target=emit)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(PluginError) as failure:
                root.close()
            self.assertEqual(failure.exception.code, 'scope_busy')
            self.assertEqual(disposed, [])
            with self.assertRaises(PluginError) as failure:
                child.emit(EventName.RUN_STARTED, {})
            self.assertEqual(failure.exception.code, 'scope_unavailable')
        finally:
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            root.close()
        self.assertEqual(failures, [])
        self.assertEqual(results, [()])
        self.assertEqual(disposed, ['parent'])

    def test_observer_can_request_drain_and_finish_nested_admitted_work(self):
        disposed, observed = [], []

        class Action:
            def invoke(self):
                return 'finished'

        def mount(context, config):
            context.provide('action', Action())
            context.defer(lambda: disposed.append(True))

            def listener(payload):
                try:
                    context.host.close()
                except PluginError as exc:
                    observed.append(exc.code)
                observed.append(context.host.call('action', 'invoke'))

            context.on(EventName.TOOL_AFTER, listener)

        host = PluginHost(profile(entry('action')), [PluginSpec('action', ('action',), (), mount)])
        try:
            self.assertEqual(host.emit(EventName.TOOL_AFTER, {}), ())
            self.assertEqual(observed, ['scope_busy', 'finished'])
            self.assertEqual(disposed, [])
        finally:
            host.close()
        self.assertEqual(disposed, [True])

    def test_copied_context_cannot_admit_a_new_thread_after_drain(self):
        failures, completed = [], []
        host = PluginHost(profile(), [])

        def request():
            try:
                completed.append(lease_once(host))
            except PluginError as exc:
                failures.append(exc.code)

        try:
            with host.lease():
                inherited = copy_context()
                host.drain()
                worker = Thread(target=lambda: inherited.run(request))
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, ['scope_unavailable'])
                self.assertEqual(completed, [])
                self.assertEqual(native_call('inspect', host=host.id)['leases'], 1)
        finally:
            host.close()

    def test_copied_context_worker_admitted_before_drain_owns_independent_lease(self):
        entered, release = Event(), Event()
        failures = []
        host = PluginHost(profile(), [])

        def request():
            try:
                with host.lease():
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('test synchronization failed')
            except BaseException as exc:
                failures.append(exc)

        worker = None
        try:
            with host.lease():
                inherited = copy_context()
                worker = Thread(target=lambda: inherited.run(request))
                worker.start()
                self.assertTrue(entered.wait(5))
                self.assertEqual(native_call('inspect', host=host.id)['leases'], 2)
                host.drain()
            self.assertEqual(native_call('inspect', host=host.id)['leases'], 1)
            with self.assertRaises(PluginError) as failure:
                host.close()
            self.assertEqual(failure.exception.code, 'scope_busy')
        finally:
            release.set()
            if worker is not None:
                worker.join(5)
                self.assertFalse(worker.is_alive())
            host.close()
        self.assertEqual(failures, [])

    def test_listener_base_exception_releases_admission_before_close(self):
        disposed = []

        def mount(context, config):
            context.defer(lambda: disposed.append(True))

            def listener(payload):
                raise KeyboardInterrupt('synthetic observer interruption')

            context.on(EventName.RUN_STARTED, listener)

        host = PluginHost(profile(entry('observer')), [PluginSpec('observer', (), (), mount)])
        try:
            with self.assertRaises(KeyboardInterrupt):
                host.emit(EventName.RUN_STARTED, {})
            self.assertEqual(native_call('inspect', host=host.id)['leases'], 0)
        finally:
            host.close()
        self.assertEqual(disposed, [True])

    def test_listener_registered_during_mount_is_not_callable_before_ready(self):
        observed, failures = [], []

        def mount(context, config):
            context.on(EventName.RUN_STARTED, lambda payload: observed.append('called'))

            def request():
                try:
                    context.host.emit(EventName.RUN_STARTED, {})
                except PluginError as exc:
                    failures.append(exc.code)

            worker = Thread(target=request)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive())

        with PluginHost(profile(entry('observer')), [PluginSpec('observer', (), (), mount)]) as host:
            self.assertEqual(observed, [])
            self.assertEqual(failures, ['scope_unavailable'])
            self.assertEqual(host.emit(EventName.RUN_STARTED, {}), ())
            self.assertEqual(observed, ['called'])
