from contextvars import copy_context
from threading import Event, Thread
import unittest

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


def profile(*plugins):
    return {'version': 1, 'plugins': list(plugins)}


class RuntimeReviewContracts(unittest.TestCase):
    def exercise_automatic_reconfiguration(self, mode):
        entered, release = Event(), Event()
        disposed, observed, results, failures = [], [], [], []
        closing = [False]

        class Stable:
            def read(self):
                return 'stable'

        def mount(context, config):
            class Resource:
                def read(self):
                    if mode == 'owner':
                        with context.lease():
                            return wait()
                    return wait()

            def wait():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('call barrier was not released')
                return config['value']

            def dispose():
                reader = Thread(target=lambda: observed.append(context.host.service('stable').read()))
                reader.start()
                reader.join(5)
                if reader.is_alive():
                    raise RuntimeError('cleanup callback ran under the metadata lock')
                if not closing[0]:
                    observed.append(context.host.call('stable', 'read'))
                disposed.append(config['value'])

            context.provide('resource', Resource())
            context.defer(dispose)

        catalog = [PluginSpec('resource', ('resource',), (), mount),
            PluginSpec('stable', ('stable',), (), lambda context, config: context.provide('stable', Stable()))]
        host = PluginHost(profile({'id': 'resource', 'use': 'resource', 'config': {'value': 1}},
            {'id': 'stable', 'use': 'stable'}), catalog)

        def invoke():
            try:
                if mode == 'scope':
                    with host.lease():
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError('scope barrier was not released')
                        results.append(1)
                elif mode == 'owner':
                    results.append(host.service('resource').read())
                else:
                    results.append(host.call('resource', 'read'))
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            pending = host.update('resource', config={'value': 2})
            self.assertEqual(pending['runtime']['state'], 'draining')
            self.assertEqual(disposed, [])
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(results, [1])
            self.assertEqual(host.inspect()['state'], 'ready')
            self.assertEqual(disposed, [1])
            self.assertEqual(observed, ['stable', 'stable'])
            self.assertEqual(host.call('resource', 'read'), 2)
        finally:
            release.set()
            worker.join(5)
            closing[0] = True
            host.close()

    def test_operation_completion_drives_pending_update_without_a_second_management_call(self):
        self.exercise_automatic_reconfiguration('invoke')

    def test_scope_lease_release_drives_pending_update_without_a_second_management_call(self):
        self.exercise_automatic_reconfiguration('scope')

    def test_owner_lease_release_drives_pending_update_without_a_second_management_call(self):
        self.exercise_automatic_reconfiguration('owner')

    def test_inherited_owner_lease_keeps_admitted_deadline_and_can_finish_after_parent_drain(self):
        for drain in (False, True):
            with self.subTest(drain=drain):
                entered, release = Event(), Event()
                calls, results, failures = [], [], []

                def mount(context, config):
                    class Service:
                        def work(self):
                            outer = context.host.current_call
                            calls.append(outer)
                            entered.set()
                            if not release.wait(5):
                                raise RuntimeError('inherited call barrier was not released')
                            with context.lease():
                                inner = context.host.call('service', 'deadline')
                                return outer['deadline'], inner

                        def deadline(self):
                            return context.host.current_call['deadline']

                    context.provide('service', Service())

                root = PluginHost(profile({'id': 'service', 'use': 'service'}),
                    [PluginSpec('service', ('service',), (), mount)])
                child = root.scope(profile())

                def invoke():
                    try:
                        results.append(child.bind('service').invoke('work', timeout=30))
                    except BaseException as error:
                        failures.append(error)

                worker = Thread(target=invoke)
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    if drain:
                        root.drain()
                    release.set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(failures, [])
                    self.assertEqual(len(results), 1)
                    self.assertIsNotNone(results[0][0])
                    self.assertEqual(results[0][1], results[0][0])
                    self.assertEqual(calls[0]['runtime'], root.runtime_id)
                    self.assertEqual(calls[0]['host'], child.id)
                finally:
                    release.set()
                    worker.join(5)
                    root.close()

    def test_prior_generation_owner_cannot_borrow_a_current_inherited_call(self):
        contexts, failures = [], []

        def mount(context, config):
            contexts.append(context)

            class Service:
                def use_old_owner(self):
                    with contexts[0].lease():
                        failures.append('stale owner admitted')

            context.provide('service', Service())

        with PluginHost(profile({'id': 'service', 'use': 'service'}),
                [PluginSpec('service', ('service',), (), mount)]) as root:
            child = root.scope(profile())
            root.update('service', config={'revision': 2})
            with self.assertRaises(PluginError) as rejected:
                child.call('service', 'use_old_owner')
            self.assertEqual(rejected.exception.code, 'stale_generation')
            self.assertEqual(failures, [])

    def test_concurrent_completions_wake_one_root_drainer_without_blocking_other_calls(self):
        entered = {name: Event() for name in ('first', 'second')}
        release = {name: Event() for name in entered}
        cleaning, finish_cleanup, second_done = Event(), Event(), Event()
        disposed, results, failures = [], [], []

        def mount(context, config):
            name = context.plugin_id

            class Service:
                def work(self):
                    entered[name].set()
                    if not release[name].wait(5):
                        raise RuntimeError('concurrent call barrier was not released')
                    return name

            def cleanup():
                if name == 'first' and config['revision'] == 1:
                    cleaning.set()
                    if not finish_cleanup.wait(5):
                        raise RuntimeError('cleanup barrier was not released')
                disposed.append((name, config['revision']))

            context.provide(name, Service())
            context.defer(cleanup)

        catalog = [PluginSpec(name, (name,), (), mount) for name in entered]
        host = PluginHost(profile(*({'id': name, 'use': name, 'config': {'revision': 1}}
            for name in entered)), catalog)

        def invoke(name):
            try:
                results.append(host.call(name, 'work'))
            except BaseException as error:
                failures.append(error)
            finally:
                if name == 'second':
                    second_done.set()

        workers = [Thread(target=invoke, args=(name,)) for name in entered]
        for worker in workers:
            worker.start()
        try:
            for event in entered.values():
                self.assertTrue(event.wait(5))
            host.update('first', config={'revision': 2})
            host.update('second', config={'revision': 2})
            release['first'].set()
            self.assertTrue(cleaning.wait(5))
            release['second'].set()
            self.assertTrue(second_done.wait(5), 'another completion blocked behind a cleanup callback')
            self.assertEqual(disposed, [])
            finish_cleanup.set()
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertCountEqual(results, ['first', 'second'])
            self.assertEqual(host.inspect()['state'], 'ready')
            self.assertCountEqual(disposed, [('first', 1), ('second', 1)])
        finally:
            for event in release.values():
                event.set()
            finish_cleanup.set()
            for worker in workers:
                worker.join(5)
            host.close()

    def test_automatic_cleanup_failure_preserves_the_call_result_until_explicit_retry(self):
        entered, release = Event(), Event()
        blocked = [True]
        attempts, mounted, results, failures = [], [], [], []

        def mount(context, config):
            mounted.append(config['revision'])

            class Service:
                def work(self):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('call barrier was not released')
                    return 'committed result'

            def cleanup():
                attempts.append(config['revision'])
                if blocked[0]:
                    raise RuntimeError('synthetic cleanup failure')

            context.provide('service', Service())
            context.defer(cleanup)

        host = PluginHost(profile({'id': 'service', 'use': 'service', 'config': {'revision': 1}}),
            [PluginSpec('service', ('service',), (), mount)])

        def invoke():
            try:
                results.append(host.call('service', 'work'))
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            host.update('service', config={'revision': 2})
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(results, ['committed result'])
            snapshot = host.inspect()
            self.assertEqual(snapshot['state'], 'cleanup_failed')
            self.assertIn('synthetic cleanup failure', snapshot['instances'][0]['failure'])
            self.assertEqual(attempts, [1])
            self.assertEqual(mounted, [1])
            blocked[0] = False
            host.retry('service')
            self.assertEqual(host.inspect()['state'], 'ready')
            self.assertEqual(attempts, [1, 1])
            self.assertEqual(mounted, [1, 2])
        finally:
            blocked[0] = False
            release.set()
            worker.join(5)
            host.close()

    def test_copied_call_context_cannot_borrow_admission_in_another_thread_after_drain(self):
        entered, release = Event(), Event()
        results, failures = [], []

        def mount(context, config):
            class Service:
                def work(self):
                    copied = copy_context()
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('copied-context barrier was not released')

                    def borrow():
                        try:
                            with context.lease():
                                failures.append('copied thread borrowed an existing admission')
                        except PluginError as error:
                            results.append(error.code)

                    worker = Thread(target=lambda: copied.run(borrow))
                    worker.start()
                    worker.join(5)
                    if worker.is_alive():
                        raise RuntimeError('copied-context worker did not finish')

            context.provide('service', Service())

        root = PluginHost(profile({'id': 'service', 'use': 'service'}),
            [PluginSpec('service', ('service',), (), mount)])
        child = root.scope(profile())

        def invoke():
            try:
                child.call('service', 'work')
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            root.drain()
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(results, ['scope_unavailable'])
        finally:
            release.set()
            worker.join(5)
            root.close()

    def test_nested_serial_owner_leases_keep_the_current_call_chain(self):
        def mount(context, config):
            class Service:
                def work(self):
                    deadline = context.host.current_call['deadline']
                    with context.lease(), context.lease():
                        return deadline, context.host.call('service', 'deadline')

                def deadline(self):
                    return context.host.current_call['deadline']

            context.provide('service', Service())

        with PluginHost(profile({'id': 'service', 'use': 'service'}),
                [PluginSpec('service', ('service',), (), mount, concurrency='serial')]) as root:
            child = root.scope(profile())
            with root.lease():
                deadline, nested = child.bind('service').invoke('work', timeout=30)
            self.assertIsNotNone(deadline)
            self.assertEqual(nested, deadline)

    def test_automatic_parent_shutdown_preserves_disposed_child_inspection(self):
        entered, release = Event(), Event()
        failures = []

        def mount(context, config):
            class Service:
                def work(self):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('shutdown barrier was not released')

            context.provide('service', Service())

        root = PluginHost(profile({'id': 'service', 'use': 'service'}),
            [PluginSpec('service', ('service',), (), mount)])
        child = root.scope(profile())

        def invoke():
            try:
                child.call('service', 'work')
            except BaseException as error:
                failures.append(error)

        worker = Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(PluginError) as busy:
                root.close()
            self.assertEqual(busy.exception.code, 'scope_busy')
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(root.inspect()['state'], 'disposed')
            root.close()
            self.assertEqual(child.inspect()['state'], 'disposed')
            child.close()
        finally:
            release.set()
            worker.join(5)
            root.close()
