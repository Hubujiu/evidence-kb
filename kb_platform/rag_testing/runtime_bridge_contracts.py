from threading import Barrier, Event, Thread
from tempfile import TemporaryDirectory
import unittest

from kb_platform.harness_services import builtin_plugins
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError, native_call
from kb_platform.session_identity import Actor
from kb_platform.session_store import SessionStore


def profile(*entries):
    return {'version': 1, 'plugins': list(entries)}


def entry(name, **fields):
    return {'id': name, 'use': name, **fields}


def value_spec(name, value):
    return PluginSpec(name, (name,), (), lambda ctx, config: ctx.provide(name, value))


class RuntimeBridgeContracts(unittest.TestCase):
    def test_inherited_call_registers_late_effect_after_drain_under_original_owner(self):
        entered, release = Event(), Event()
        closed, failures = [], []
        def mount(ctx, config):
            class Resource:
                def create(self):
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('test barrier timed out')
                    ctx.effect(lambda: closed.append('late'))
            ctx.defer(lambda: closed.append('initial'))
            ctx.provide('resource', Resource())
        root = PluginHost(profile(entry('resource')), [PluginSpec('resource', ('resource',), (), mount)])
        child = root.scope(profile())
        def invoke():
            try:
                child.call('resource', 'create')
            except BaseException as exc:
                failures.append(exc)
        worker = Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            root.drain()
            with self.assertRaises(PluginError):
                root.close()
            self.assertEqual(closed, [])
        finally:
            release.set()
            worker.join(5)
            root.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(closed, ['late', 'initial'])

    def test_real_sqlite_session_commit_reopens_after_rust_disposes_host(self):
        actor = Actor('synthetic-tenant', 'synthetic-user')
        with TemporaryDirectory() as root:
            host = PluginHost(profile({'id': 'store', 'use': 'harness.store.sqlite',
                'config': {'root': root}}), builtin_plugins())
            session = host.call('session.store', 'create', actor, 'scope', {'value': 1})
            sid = session['session_id']
            lease = host.call('session.store', 'begin', actor, sid, 'request', 0, 'fingerprint')
            host.call('session.store', 'finish', actor, sid, lease['token'], {'value': 2},
                {'status': 'complete', 'value': 2}, assistant_event={'role': 'assistant', 'content': '2'})
            host.close()
            self.assertEqual(host.inspect()['state'], 'disposed')
            self.assertEqual(SessionStore(root).load(actor, sid)['state'], {'value': 2})
            self.assertEqual(SessionStore(root).begin(actor, sid, 'request', 0, 'fingerprint'),
                {'cached': {'status': 'complete', 'value': 2, 'session_id': sid, 'revision': 1}})
            with self.assertRaises(PluginError):
                host.call('session.store', 'load', actor, sid)

    def test_replaced_blocked_init_reclaims_late_resources_and_publishes_only_new_generation(self):
        entered, release = Event(), Event()
        contexts, disposed, hosts, failures = [], [], [], []
        def mount(ctx, config):
            contexts.append(ctx)
            if config['value'] == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test barrier timed out')
            ctx.defer(lambda: disposed.append(config['value']))
            ctx.provide('value', config['value'])
        spec = PluginSpec('value', ('value',), (), mount)
        def start():
            try:
                hosts.append(PluginHost(profile(entry('value', config={'value': 1})), [spec]))
            except BaseException as exc:
                failures.append(exc)
        worker = Thread(target=start)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            host = contexts[0].host
            host.update('value', config={'value': 2})
            self.assertEqual(host.inspect()['instances'][0]['state'], 'draining')
            self.assertEqual(disposed, [])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        try:
            self.assertEqual(hosts[0].service('value'), 2)
            self.assertEqual(disposed, [1])
            self.assertGreater(contexts[1].identity['generation'], contexts[0].identity['generation'])
        finally:
            hosts[0].close()
        self.assertEqual(disposed, [1, 2])

    def test_blocked_python_call_cancel_keeps_resources_until_actual_return(self):
        entered, release = Event(), Event()
        operations, disposed, results, failures = [], [], [], []
        class Slow:
            def invoke(self):
                operations.append(host.current_call)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test barrier timed out')
                return 'completed'
        def mount(ctx, config):
            ctx.provide('slow', Slow())
            ctx.defer(lambda: disposed.append('closed'))
        host = PluginHost(profile(entry('slow')), [PluginSpec('slow', ('slow',), (), mount)])
        def invoke():
            try:
                results.append(host.call('slow', 'invoke'))
            except BaseException as exc:
                failures.append(exc)
        worker = Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            result = host.cancel(operations[0]['operation'])
            self.assertFalse(result['complete'])
            self.assertTrue(operations[0]['cancel_signal'].is_set())
            with self.assertRaises(PluginError):
                host.close()
            self.assertEqual(disposed, [])
            self.assertEqual(host.inspect()['leases'], 1)
        finally:
            release.set()
            worker.join(5)
            host.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, ['completed'])
        self.assertEqual(disposed, ['closed'])

    def test_pending_dependency_activates_on_apply_and_unrelated_identity_is_retained(self):
        consumer = PluginSpec('consumer', ('result',), ('value',),
            lambda ctx, config: ctx.provide('result', ctx.require('value') + 1))
        with PluginHost(profile(entry('consumer'), entry('stable')),
                [consumer, value_spec('value', 7), value_spec('stable', 9)], dynamic=True) as host:
            stable = host.bind('stable').identity
            self.assertEqual(host.inspect()['state'], 'pending')
            host.apply(profile(entry('consumer'), entry('stable'), entry('value')))
            self.assertEqual(host.service('result'), 8)
            self.assertEqual(host.bind('stable').identity, stable)

    def test_same_shape_implementation_switch_invalidates_old_binding_only(self):
        class Number:
            def __init__(self, value):
                self.value = value
            def get(self):
                return self.value
        catalog = [PluginSpec(key, ('number',), (),
            lambda ctx, config, value=value: ctx.provide('number', Number(value)))
            for key, value in [('first', 1), ('second', 2)]]
        with PluginHost(profile({'id': 'n', 'use': 'first'}), catalog) as host:
            old = host.bind('number')
            self.assertEqual(old.call('get'), 1)
            host.apply(profile({'id': 'n', 'use': 'second'}))
            self.assertEqual(host.call('number', 'get'), 2)
            with self.assertRaises(PluginError):
                old.call('get')

    def test_invalid_apply_has_no_effect_and_cleanup_failure_needs_explicit_retry(self):
        attempts, blocked = [], [True]
        def mount(ctx, config):
            ctx.provide('value', config.get('value', 1))
            def close():
                attempts.append('close')
                if blocked[0]:
                    raise RuntimeError('synthetic cleanup busy')
            ctx.defer(close)
        catalog = [PluginSpec('value', ('value',), (), mount),
            PluginSpec('broken', (), ('missing',), lambda ctx, config: None)]
        host = PluginHost(profile(entry('value')), catalog)
        try:
            before = host.inspect()['desired_revision']
            with self.assertRaises(PluginError):
                host.apply(profile(entry('value'), entry('broken')))
            self.assertEqual(host.inspect()['desired_revision'], before)
            self.assertEqual(attempts, [])
            with self.assertRaises(ExceptionGroup):
                host.update('value', enabled=False)
            host.update('value', enabled=True, config={'value': 2})
            self.assertEqual(attempts, ['close'])
            self.assertEqual(host.inspect()['state'], 'cleanup_failed')
            blocked[0] = False
            host.retry('value')
            self.assertEqual(host.service('value'), 2)
            self.assertEqual(attempts, ['close', 'close'])
        finally:
            blocked[0] = False
            host.close()

    def test_event_modes_preserve_order_bail_values_waterfall_and_next_guard(self):
        def mount(ctx, config):
            ctx.on('serial', lambda value: value['value'] + 1)
            ctx.on('serial', lambda value: value['value'] + 2)
            ctx.on('bail', lambda value: None)
            ctx.on('bail', lambda value: False)
            ctx.on('bail', lambda value: 0)
            ctx.on('bail', lambda value: self.fail('bail dispatched after a result'))
            ctx.on('waterfall', lambda value, next_: next_(value + 1) + 4)
            ctx.on('waterfall', lambda value, next_: next_(value * 2))
            def repeated(value, next_):
                next_()
                return next_()
            ctx.on('repeated', repeated)
        with PluginHost(profile(entry('events')), [PluginSpec('events', (), (), mount)]) as host:
            self.assertEqual(host.dispatch('serial', {'value': 3}, mode='serial'), 4)
            self.assertEqual(host.dispatch('bail', {}, mode='bail'), 0)
            self.assertEqual(host.dispatch('waterfall', 2, mode='waterfall'), 10)
            with self.assertRaises(PluginError) as failure:
                host.dispatch('repeated', 2, mode='waterfall')
            self.assertEqual(failure.exception.code, 'WATERFALL_NEXT_REUSED')

    def test_parallel_listeners_finish_and_failures_are_recorded_before_return(self):
        barrier, seen = Barrier(2), []
        def mount(ctx, config):
            def good(value):
                barrier.wait(5)
                seen.append('good')
                return 1
            def bad(value):
                barrier.wait(5)
                seen.append('bad')
                raise ValueError('synthetic listener failure')
            ctx.on('parallel', good)
            ctx.on('parallel', bad)
        with PluginHost(profile(entry('events')), [PluginSpec('events', (), (), mount)]) as host:
            with self.assertRaises(ExceptionGroup):
                host.dispatch('parallel', {}, mode='parallel')
            self.assertCountEqual(seen, ['good', 'bad'])
            self.assertEqual(host.inspect()['leases'], 0)
            self.assertTrue(any(row['code'] == 'listener_failed' for row in host.inspect()['diagnostics']))
            host.update('events', enabled=False)
            self.assertEqual(host.dispatch('parallel', {}, mode='parallel'), ())

    def test_child_isolation_and_intercept_keep_binding_domains_explicit(self):
        with PluginHost(profile(entry('value')), [value_spec('value', 1)],
                intercept={'value': {'limit': 4, 'options': {'a': 1}}}) as root:
            with root.scope(profile(), intercept={'value': {'options': {'b': 2}}}) as child:
                binding = child.bind('value').identity
                self.assertEqual(binding['definition_scope'], root.id)
                self.assertEqual(binding['call_scope'], child.id)
                self.assertEqual(binding['intercept'], {'limit': 4, 'options': {'a': 1, 'b': 2}})
            with root.scope(profile(), isolated=['value'], dynamic=True) as isolated:
                with self.assertRaises(PluginError):
                    isolated.service('value')
                with self.assertRaises(PluginError):
                    native_call('runtime_acquire', host=isolated.id,
                        registration=root.bind('value').identity['registration'])
