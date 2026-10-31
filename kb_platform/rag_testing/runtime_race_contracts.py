from hashlib import sha256
from threading import Event, RLock, Thread, Timer, current_thread
import unittest
from unittest.mock import patch

from kb_platform import plugin_host
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


class RuntimeRaceContracts(unittest.TestCase):
    def test_concurrent_updates_keep_native_configuration_and_python_inputs_consistent(self):
        committed, second_lock_attempt = Event(), Event()
        failures = []
        lock = RLock()

        class OrderedLock:
            def __enter__(self):
                if current_thread().name == 'second-update':
                    second_lock_attempt.set()
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        def mount(context, config):
            class Service:
                def value(self):
                    return config['value']
            context.provide('service', Service())

        def inputs(config):
            return {'value': sha256(str(config['value']).encode()).hexdigest()}

        profile = {'version': 1, 'plugins': [{'id': 'p', 'use': 'p', 'config': {'value': 0}}]}
        host = PluginHost(profile, [PluginSpec('p', ('service',), (), mount, config_inputs=inputs)])
        host._lock = OrderedLock()
        native = plugin_host.native_call

        def interleave(command, **fields):
            result = native(command, **fields)
            if command == 'runtime_update' and current_thread().name == 'first-update':
                committed.set()
                if not second_lock_attempt.wait(5):
                    raise AssertionError('second update did not reach the metadata lock')
            return result

        def update(value):
            try:
                host.update('p', config={'value': value})
            except BaseException as error:
                failures.append(error)

        first = Thread(target=update, args=(1,), name='first-update')
        second = Thread(target=update, args=(2,), name='second-update')
        try:
            with patch.object(plugin_host, 'native_call', interleave):
                first.start()
                self.assertTrue(committed.wait(5))
                second.start()
                first.join(5)
                second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(host.inspect()['plugins'][0]['config'], {'value': 2})
            self.assertEqual(host._entries['p'][1], {'value': 2})
            self.assertEqual(host.call('service', 'value'), 2)
            host.update('p', enabled=False)
            host.update('p', enabled=True)
            self.assertEqual(host.call('service', 'value'), 2)
        finally:
            second_lock_attempt.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
            host.close()

    def test_update_cancellation_cannot_be_consumed_before_signal_registration(self):
        admitted, register, update_finished = Event(), Event(), Event()
        results, failures, polled = [], [], []
        lock = RLock()

        class AdmissionLock:
            def __enter__(self):
                if current_thread().name == 'configuration-update':
                    if lock.acquire(blocking=False):
                        return
                    register.set()
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        def mount(context, config):
            class Service:
                def work(self):
                    if not update_finished.wait(5):
                        raise AssertionError('update did not complete cancellation delivery')
                    return context.host.current_call['cancel_signal'].is_set()
            context.provide('service', Service())

        profile = {'version': 1, 'plugins': [{'id': 'p', 'use': 'p'}]}
        host = PluginHost(profile, [PluginSpec('p', ('service',), (), mount)])
        host._lock = AdmissionLock()
        native = plugin_host.native_call

        def interleave(command, **fields):
            result = native(command, **fields)
            if command == 'runtime_invoke':
                admitted.set()
                if not register.wait(5):
                    raise AssertionError('admission was not released')
            elif command == 'runtime_poll':
                cancellations = [action for action in result['actions'] if action['kind'] == 'cancel']
                if polled and not cancellations:
                    register.set()
                polled.extend(cancellations)
            return result

        def invoke():
            try:
                results.append(host.call('service', 'work'))
            except BaseException as error:
                failures.append(error)

        def update():
            try:
                host.update('p', config={'revision': 2})
            except BaseException as error:
                failures.append(error)
            finally:
                update_finished.set()

        worker = Thread(target=invoke)
        updater = Thread(target=update, name='configuration-update')
        try:
            with patch.object(plugin_host, 'native_call', interleave):
                worker.start()
                self.assertTrue(admitted.wait(5))
                updater.start()
                worker.join(5)
                updater.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(updater.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(polled), 1)
            self.assertEqual(results, [True])
            self.assertEqual(host.inspect()['leases'], 0)
            self.assertEqual(host.inspect()['state'], 'ready')
        finally:
            register.set()
            update_finished.set()
            worker.join(5)
            if updater.ident is not None:
                updater.join(5)
            host.close()

    def test_configuration_callbacks_release_metadata_lock_and_revalidate_stale_inputs(self):
        validating, release, updated = Event(), Event(), Event()
        failures = []
        observed = []

        def inputs(config):
            if current_thread().name == 'enable-with-prior-config':
                observed.append(config['value'])
                if len(observed) == 1:
                    validating.set()
                    if not release.wait(5):
                        raise AssertionError('configuration validation did not resume')
            return {'value': sha256(str(config['value']).encode()).hexdigest()}

        def mount(context, config):
            context.provide('service', config['value'])

        profile = {'version': 1, 'plugins': [{'id': 'p', 'use': 'p', 'config': {'value': 0}}]}
        host = PluginHost(profile, [PluginSpec('p', ('service',), (), mount, config_inputs=inputs)])

        def enable():
            try:
                host.update('p', enabled=True)
            except BaseException as error:
                failures.append(error)

        def update():
            try:
                host.update('p', config={'value': 2})
            except BaseException as error:
                failures.append(error)
            finally:
                updated.set()

        enabler = Thread(target=enable, name='enable-with-prior-config')
        updater = Thread(target=update)
        try:
            enabler.start()
            self.assertTrue(validating.wait(5))
            updater.start()
            self.assertTrue(updated.wait(5), 'user configuration callback held the metadata lock')
            release.set()
            enabler.join(5)
            updater.join(5)
            self.assertFalse(enabler.is_alive())
            self.assertFalse(updater.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(observed, [0, 2])
            self.assertEqual(host.service('service'), 2)
            self.assertEqual(host.inspect()['state'], 'ready')
        finally:
            release.set()
            enabler.join(5)
            if updater.ident is not None:
                updater.join(5)
            host.close()

    def test_call_initialization_failures_release_admission_and_cancellation_state(self):
        timers = []

        class RecordingTimer(Timer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                timers.append(self)

        class CannotStartTimer(RecordingTimer):
            def start(self):
                raise RuntimeError("can't start new thread")

        class CannotSetContext:
            def __init__(self, context):
                self.context = context

            def get(self):
                return self.context.get()

            def set(self, value):
                raise RuntimeError('context initialization failed')

            def reset(self, token):
                self.context.reset(token)

        def cannot_create_event():
            raise RuntimeError('event initialization failed')

        cases = [('Event', cannot_create_event),
                 ('Timer', CannotStartTimer),
                 ('_current_call', CannotSetContext(plugin_host._current_call)),
                 ('_leases', CannotSetContext(plugin_host._leases))]
        for attribute, replacement in cases:
            with self.subTest(initialization=attribute):
                called = []

                def mount(context, config):
                    class Service:
                        def work(self):
                            called.append(True)
                    context.provide('service', Service())

                profile = {'version': 1, 'plugins': [{'id': 'p', 'use': 'p'}]}
                host = PluginHost(profile, [PluginSpec('p', ('service',), (), mount)])
                admitted = []
                native = plugin_host.native_call

                def record(command, **fields):
                    result = native(command, **fields)
                    if command == 'runtime_invoke':
                        admitted.append(result['operation'])
                    return result

                call_token = plugin_host._current_call.set(None)
                leases_token = plugin_host._leases.set(())
                try:
                    with patch.object(plugin_host, 'native_call', record), \
                            patch.object(plugin_host, 'Timer', RecordingTimer):
                        with patch.object(plugin_host, attribute, replacement):
                            with self.assertRaises(RuntimeError):
                                host.bind('service').invoke('work', timeout=10)
                    self.assertEqual(called, [])
                    self.assertEqual(host.inspect()['counts']['leases'], 0)
                    self.assertEqual(host.inspect()['counts']['pending_operations'], 0)
                    self.assertEqual(host._cancellations, {})
                    self.assertIsNone(host.current_call)
                    self.assertEqual(plugin_host._leases.get(), ())
                finally:
                    if host.inspect()['counts']['pending_operations']:
                        for operation in admitted:
                            native('runtime_complete', host=host.id, operation=operation,
                                   status='error', error='fixture cleanup')
                            native('runtime_ack', host=host.id, operations=[operation])
                    for timer in timers:
                        timer.cancel()
                        if timer.ident is not None:
                            timer.join(5)
                    plugin_host._current_call.reset(call_token)
                    plugin_host._leases.reset(leases_token)
                    host._cancellations.clear()
                    host.close()

    def test_manual_cancel_notifies_running_call_while_sibling_cleanup_blocks(self):
        entered, cleanup_entered, release, release_cleanup = Event(), Event(), Event(), Event()
        operations, results, failures, disposed = [], [], [], []

        def worker_plugin(context, config):
            class Service:
                def work(self):
                    operations.append(context.host.current_call)
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('test did not release the running call')
                    return 'finished'

            context.provide('worker', Service())
            context.defer(lambda: disposed.append('worker'))

        def blocker_plugin(context, config):
            def cleanup():
                cleanup_entered.set()
                if not release_cleanup.wait(5):
                    raise AssertionError('test did not release sibling cleanup')
                disposed.append('blocker')

            context.defer(cleanup)

        root = PluginHost({'version': 1, 'plugins': []}, [])
        worker_host = root.scope({'version': 1, 'plugins': [{'id': 'worker', 'use': 'worker'}]},
            [PluginSpec('worker', ('worker',), (), worker_plugin)])
        blocker_host = root.scope({'version': 1, 'plugins': [{'id': 'blocker', 'use': 'blocker'}]},
            [PluginSpec('blocker', (), (), blocker_plugin)])

        def invoke():
            try:
                results.append(worker_host.call('worker', 'work'))
            except BaseException as error:
                failures.append(error)

        def disable():
            try:
                blocker_host.update('blocker', enabled=False)
            except BaseException as error:
                failures.append(error)

        worker, cleaner = Thread(target=invoke), Thread(target=disable)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            cleaner.start()
            self.assertTrue(cleanup_entered.wait(5))
            result = worker_host.cancel(operations[0]['operation'])
            self.assertTrue(result['cancelled'])
            self.assertFalse(result['complete'])
            self.assertTrue(operations[0]['cancel_signal'].is_set(),
                            'manual cancellation must notify before unrelated cleanup returns')
            self.assertTrue(cleaner.is_alive())
            self.assertEqual(worker_host.inspect()['leases'], 1)
            self.assertEqual(disposed, [])
        finally:
            release.set()
            release_cleanup.set()
            worker.join(5)
            if cleaner.ident is not None:
                cleaner.join(5)
            root.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(cleaner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, ['finished'])
        self.assertCountEqual(disposed, ['blocker', 'worker'])

    def test_sibling_update_failure_does_not_dispose_successfully_mounted_scope(self):
        entered, release = Event(), Event()
        hosts, failures, disposed = [], [], []

        def unstable(context, config):
            if config.get('fail'):
                raise ValueError('sibling reconfiguration failed')
            context.provide('unstable', 'initial')

        def stable(context, config):
            context.defer(lambda: disposed.append('stable'))
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release stable mount')
            context.provide('stable', 'ready')

        root = PluginHost({'version': 1, 'plugins': []}, [])
        sibling = root.scope({'version': 1, 'plugins': [{'id': 'unstable', 'use': 'unstable'}]},
            [PluginSpec('unstable', ('unstable',), (), unstable)])

        def create():
            try:
                hosts.append(root.scope({'version': 1, 'plugins': [{'id': 'stable', 'use': 'stable'}]},
                    [PluginSpec('stable', ('stable',), (), stable)]))
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=create)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            sibling.update('unstable', config={'fail': True})
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [], 'failure from sibling update escaped through stable constructor')
            self.assertEqual(len(hosts), 1)
            self.assertEqual(hosts[0].service('stable'), 'ready')
            self.assertEqual(hosts[0].inspect()['state'], 'ready')
            self.assertEqual(disposed, [])
            failed = sibling.inspect()
            self.assertEqual(failed['state'], 'failed')
            self.assertEqual(failed['instances'][0]['failure'], 'ValueError: sibling reconfiguration failed')
        finally:
            release.set()
            worker.join(5)
            root.close()
        self.assertEqual(disposed, ['stable'])

    def test_nested_python_call_receives_its_inherited_deadline_notification(self):
        observations = []

        def mount(context, config):
            class Service:
                def outer(self):
                    return context.host.call('service', 'inner')

                def inner(self):
                    invocation = context.host.current_call
                    observations.append(invocation['deadline'])
                    return invocation['cancel_signal'].wait(2)

            context.provide('service', Service())

        profile = {'version': 1, 'plugins': [{'id': 'service', 'use': 'service'}]}
        with PluginHost(profile, [PluginSpec('service', ('service',), (), mount)]) as host:
            self.assertTrue(host.bind('service').invoke('outer', timeout=0.02),
                            'nested invocation must receive the inherited deadline notification')
            self.assertIsNotNone(observations[0])
            self.assertEqual(host.inspect()['leases'], 0)

    def test_python_deadline_notifies_and_retains_resources_until_actual_return(self):
        entered, notified, release = Event(), Event(), Event()
        closed, results, failures = [], [], []

        def mount(context, config):
            class Service:
                def work(self):
                    entered.set()
                    if not context.host.current_call['cancel_signal'].wait(2):
                        raise AssertionError('deadline did not notify the running Python call')
                    notified.set()
                    if not release.wait(2):
                        raise AssertionError('test did not release the running Python call')
                    return 'completed after cancellation notification'

            context.defer(lambda: closed.append('service'))
            context.provide('service', Service())

        profile = {'version': 1, 'plugins': [{'id': 'service', 'use': 'service'}]}
        host = PluginHost(profile, [PluginSpec('service', ('service',), (), mount)])

        def invoke():
            try:
                results.append(host.bind('service').invoke('work', timeout=0.02))
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=invoke, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(notified.wait(2), 'deadline must notify without another native call')
            self.assertEqual(host.inspect()['leases'], 1)
            with self.assertRaises(PluginError):
                host.close()
            self.assertEqual(closed, [])
            self.assertEqual(host.inspect()['leases'], 1)
        finally:
            release.set()
            worker.join(2)
            host.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, ['completed after cancellation notification'])
        self.assertEqual(closed, ['service'])
        self.assertEqual(host.inspect()['state'], 'disposed')

    def test_parallel_listener_finishes_nested_dependency_call_after_drain(self):
        entered, release_nested, nested_finished, release_listener = Event(), Event(), Event(), Event()
        closed, calls, results, failures = [], [], [], []

        def provider(context, config):
            class Service:
                def work(self):
                    calls.append('dependency')
                    return 'dependency result'

            context.defer(lambda: closed.append('provider'))
            context.provide('dependency', Service())

        def listener(context, config):
            def callback(payload):
                entered.set()
                if not release_nested.wait(2):
                    raise AssertionError('test did not release nested invocation')
                with context.host.lease():
                    value = context.host.call('dependency', 'work')
                nested_finished.set()
                if not release_listener.wait(2):
                    raise AssertionError('test did not release listener completion')
                return value

            context.defer(lambda: closed.append('listener'))
            context.on('example', callback)

        profile = {'version': 1, 'plugins': [
            {'id': 'provider', 'use': 'provider'}, {'id': 'listener', 'use': 'listener'}]}
        host = PluginHost(profile, [PluginSpec('provider', ('dependency',), (), provider),
                                    PluginSpec('listener', (), ('dependency',), listener)])

        def dispatch():
            try:
                results.append(host.dispatch('example', {}, mode='parallel'))
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=dispatch, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            host.drain()
            with self.assertRaises(PluginError):
                host.call('dependency', 'work')
            release_nested.set()
            self.assertTrue(nested_finished.wait(2), 'admitted listener lost its dependency authority')
            self.assertEqual(calls, ['dependency'])
            with self.assertRaises(PluginError):
                host.close()
            self.assertEqual(closed, [])
        finally:
            release_nested.set()
            release_listener.set()
            worker.join(2)
            host.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [('dependency result',)])
        self.assertEqual(closed, ['listener', 'provider'])
        self.assertEqual(host.inspect()['state'], 'disposed')
