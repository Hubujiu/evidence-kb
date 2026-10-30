from threading import Event, Thread
import unittest

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


class RuntimeRaceContracts(unittest.TestCase):
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
