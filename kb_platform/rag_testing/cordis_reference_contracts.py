from hashlib import sha256
import json
from pathlib import Path
from threading import Event
import unittest

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


ROOT = Path(__file__).resolve().parents[2]


class CordisReferenceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((ROOT / 'config/plugin-runtime/cordis-traces-v1.json').read_text('utf-8'))
        cls.scenarios = {item['id']: item for item in cls.fixture['scenarios']}

    def test_reference_identity_and_harness_are_locked(self):
        lockfile = ROOT / 'config/plugin-runtime/cordis-reference-lock.json'
        locktext = lockfile.read_text('utf-8')
        lock = json.loads(locktext)
        self.assertEqual(self.fixture['source']['commit'], 'ce7073d51254c26cd63b22a60835b8f948deb447')
        self.assertEqual(self.fixture['source']['lock_text_sha256'], sha256(locktext.encode()).hexdigest())
        self.assertEqual(len(self.scenarios), 9)
        self.assertEqual(len(lock['files']), 20)
        self.assertEqual(lock['runtime_dependencies'], ['@deepseek-ai/cosmokit'])
        self.assertEqual(lock['packages']['@deepseek-ai/cordis']['upstream_version'], '4.0.0-rc.11')
        for name, expected in lock['harness_text_sha256'].items():
            self.assertEqual(sha256((ROOT / name).read_text('utf-8').encode()).hexdigest(), expected)

    def test_dependency_cleanup_observations_match_reference(self):
        events, alive = [], [True]

        class Service:
            def read(self):
                return 'open' if alive[0] else 'closed'

        def provider(ctx, config):
            events.append('provider.start')
            ctx.provide('fixture', Service())

            def stop():
                alive[0] = False
                events.append('provider.stop')

            ctx.defer(stop)

        def consumer(ctx, config):
            service = ctx.require('fixture')
            events.extend(['consumer.start', f'read:{service.read()}'])
            ctx.defer(lambda: events.extend([f'read:{service.read()}', 'consumer.stop']))

        profile = {'version': 1, 'plugins': [{'id': 'provider', 'use': 'provider'},
            {'id': 'consumer', 'use': 'consumer'}]}
        host = PluginHost(profile, [PluginSpec('provider', ('fixture',), (), provider),
            PluginSpec('consumer', (), ('fixture',), consumer)])
        host.close()
        self.assertEqual(events, self.scenarios['dependency_cleanup_order']['events'])

    def test_dynamic_dependency_observations_match_reference(self):
        events = []

        def consumer(ctx, config):
            events.append(f"consumer.start:{ctx.require('fixture')}")
            ctx.defer(lambda: events.append('consumer.stop'))

        def provider(ctx, config):
            events.append('provider.start')
            ctx.provide('fixture', 'ready')
            ctx.defer(lambda: events.append('provider.stop'))

        catalog = [PluginSpec('provider', ('fixture',), (), provider),
            PluginSpec('consumer', (), ('fixture',), consumer)]
        pending = {'version': 1, 'plugins': [{'id': 'consumer', 'use': 'consumer'}]}
        with PluginHost(pending, catalog, dynamic=True) as host:
            self.assertEqual(events, [])
            events.append('consumer.pending')
            host.apply({'version': 1, 'plugins': [*pending['plugins'], {'id': 'provider', 'use': 'provider'}]})
            host.update('provider', enabled=False)
            events.append('consumer.pending_again')
        self.assertEqual(events, self.scenarios['dynamic_dependency_appearance_and_removal']['events'])

    def test_scope_observations_match_reference(self):
        events = []

        def mount(label):
            def activate(ctx, config):
                events.append(f'{label}.start')
                ctx.provide('fixture', label)
                ctx.defer(lambda: events.append(f'{label}.stop'))
            return activate

        catalog = [PluginSpec('parent', ('fixture',), (), mount('parent')),
            PluginSpec('child', ('fixture',), (), mount('child'))]
        with PluginHost({'version': 1, 'plugins': [{'id': 'parent', 'use': 'parent'}]}, catalog) as host:
            with host.scope({'version': 1, 'plugins': [{'id': 'child', 'use': 'child',
                    'overrides': ['fixture']}]}) as child:
                values = [host.service('fixture'), child.service('fixture')]
            values.append(host.service('fixture'))
            events.append('parent.still_available')
        expected = self.scenarios['scope_override_and_borrowed_parent']
        self.assertEqual(values, expected['values'])
        self.assertEqual(events, expected['events'])

    def test_emit_order_and_listener_ownership_match_reference(self):
        events = []

        def mount(ctx, config):
            ctx.on('fixture', lambda value: events.append(f'a:{value}'))
            ctx.on('fixture', lambda value: events.append(f'b:{value}'))

        with PluginHost({'version': 1, 'plugins': [{'id': 'listeners', 'use': 'listeners'}]},
                [PluginSpec('listeners', (), (), mount)]) as host:
            host.emit('fixture', 2)
            host.update('listeners', enabled=False)
            host.emit('fixture', 3)
        self.assertEqual(events, self.scenarios['emit_order_and_listener_ownership']['events'])

    def test_parallel_partial_order_matches_reference(self):
        events, gate = [], Event()

        def first(value):
            events.append('a.start')
            self.assertTrue(gate.wait(3), 'parallel dispatch did not start the second callback')
            events.append('a.done')

        def second(value):
            events.extend(['b.start', 'b.done'])
            gate.set()

        def mount(ctx, config):
            ctx.on('fixture', first)
            ctx.on('fixture', second)

        with PluginHost({'version': 1, 'plugins': [{'id': 'listeners', 'use': 'listeners'}]},
                [PluginSpec('listeners', (), (), mount)]) as host:
            host.dispatch('fixture', {}, mode='parallel')
        expected = self.scenarios['parallel_waits_for_every_listener']
        self.assertCountEqual(events, expected['events'])
        for before, after in expected['required_before']:
            self.assertLess(events.index(before), events.index(after))

    def test_serial_and_bail_strict_predicate_match_reference(self):
        for mode in ('serial', 'bail'):
            events = []

            def mount(ctx, config):
                for label, result in [('false', False), ('none', None), ('zero', 0), ('unreachable', 9)]:
                    def listener(value, label=label, result=result):
                        events.append(label)
                        return result
                    ctx.on('fixture', listener)

            with self.subTest(mode=mode), PluginHost(
                    {'version': 1, 'plugins': [{'id': 'listeners', 'use': 'listeners'}]},
                    [PluginSpec('listeners', (), (), mount)]) as host:
                result = host.dispatch('fixture', {}, mode=mode)
            expected = self.scenarios[f'{mode}_strict_bail_predicate']
            self.assertEqual(events, expected['events'])
            self.assertEqual(result, expected['result'])

    def test_waterfall_middleware_matches_reference(self):
        events = []

        def outer(value, next_):
            events.append('outer.before')
            result = next_()
            events.append('outer.after')
            return result + 1

        def inner(value, next_):
            events.append('inner.before')
            result = next_()
            events.append('inner.after')
            return result * 2

        def mount(ctx, config):
            ctx.on('fixture', outer)
            ctx.on('fixture', inner)

        with PluginHost({'version': 1, 'plugins': [{'id': 'listeners', 'use': 'listeners'}]},
                [PluginSpec('listeners', (), (), mount)]) as host:
            result = host.dispatch('fixture', 3, mode='waterfall')
        expected = self.scenarios['waterfall_middleware']
        self.assertEqual(events, expected['events'])
        self.assertEqual(result, expected['result'])

    def test_waterfall_repeated_next_matches_reference(self):
        def mount(ctx, config):
            def callback(value, next_):
                next_()
                return next_()
            ctx.on('fixture', callback)

        with PluginHost({'version': 1, 'plugins': [{'id': 'listeners', 'use': 'listeners'}]},
                [PluginSpec('listeners', (), (), mount)]) as host:
            with self.assertRaises(PluginError):
                host.dispatch('fixture', 1, mode='waterfall')
        self.assertTrue(self.scenarios['waterfall_repeated_next']['rejected'])
