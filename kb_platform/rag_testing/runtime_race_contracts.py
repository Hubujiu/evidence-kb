from threading import Event, Thread
import unittest

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


class RuntimeRaceContracts(unittest.TestCase):
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
