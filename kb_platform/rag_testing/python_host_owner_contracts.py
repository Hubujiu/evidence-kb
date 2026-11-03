import unittest
from unittest.mock import patch

from kb_platform import plugin_host
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


class PythonHostOwnerContracts(unittest.TestCase):
    def host(self):
        contexts = []
        def mount(context, config):
            contexts.append(context)
        host = PluginHost({'version': 1, 'plugins': [{'id': 'p', 'use': 'p'}]},
                          [PluginSpec('p', (), (), mount)])
        self.addCleanup(host.close)
        return host, contexts[0]

    def fail_context(self):
        original = plugin_host._leases
        class FailedContext:
            def get(self):
                return original.get()
            def set(self, value):
                raise MemoryError('controlled ContextVar allocation failure')
            def reset(self, token):
                original.reset(token)
        return patch.object(plugin_host, '_leases', FailedContext())

    def test_new_host_lease_setup_failure_releases_native_acquisition(self):
        host, _ = self.host()
        with self.fail_context(), self.assertRaises(MemoryError):
            with host.lease():
                self.fail('setup failure must not enter body')
        self.assertEqual(host.inspect()['counts']['leases'], 0)
        self.assertEqual(plugin_host._leases.get(), ())

    def test_reused_lease_setup_failure_restores_outer_user_count(self):
        host, _ = self.host()
        with host.lease():
            outer = host._authority()
            with self.fail_context(), self.assertRaises(MemoryError):
                with host.lease():
                    self.fail('setup failure must not enter body')
            self.assertEqual(outer.users, 1)
            self.assertIs(host._authority(), outer)
            self.assertEqual(host.inspect()['counts']['leases'], 1)
        self.assertEqual(host.inspect()['counts']['leases'], 0)

    def test_instance_lease_setup_failure_releases_native_acquisition(self):
        host, context = self.host()
        with self.fail_context(), self.assertRaises(MemoryError):
            with context.lease():
                self.fail('setup failure must not enter body')
        self.assertEqual(host.inspect()['counts']['leases'], 0)
        self.assertEqual(plugin_host._leases.get(), ())

    def test_nested_lease_exception_restores_authority_and_single_release(self):
        host, context = self.host()
        calls = []
        native = plugin_host.native_call
        def observe(command, **fields):
            if command == 'runtime_release':
                calls.append(fields['lease'])
            return native(command, **fields)
        with patch.object(plugin_host, 'native_call', observe):
            with host.lease():
                outer = host._authority()
                with self.assertRaisesRegex(ValueError, 'body'):
                    with host.lease(), context.lease():
                        raise ValueError('body')
                self.assertIs(host._authority(), outer)
                self.assertEqual(outer.users, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(set(calls)), 2)
        self.assertEqual(host.inspect()['counts']['leases'], 0)

    def test_native_lease_pins_resources_between_acquire_and_context_publication(self):
        from threading import Thread
        for instance in (False, True):
            with self.subTest(instance=instance):
                host, context = self.host()
                original = plugin_host._leases
                outcomes = []
                def close():
                    try:
                        host.close()
                    except PluginError as exc:
                        outcomes.append(exc.code)
                class InterleavedContext:
                    def get(self):
                        return original.get()
                    def set(self, value):
                        worker = Thread(target=close)
                        worker.start()
                        worker.join(5)
                        if worker.is_alive():
                            raise AssertionError('close held while context publication was paused')
                        return original.set(value)
                    def reset(self, token):
                        return original.reset(token)
                with patch.object(plugin_host, '_leases', InterleavedContext()):
                    with (context.lease() if instance else host.lease()):
                        self.assertEqual(len(outcomes), 1)
                        self.assertEqual('scope_busy', outcomes[0])
                        self.assertEqual(host.inspect()['counts']['leases'], 1)
                        self.assertTrue(host._contexts)
                self.assertEqual(host.inspect()['counts']['leases'], 0)

    def test_callback_failure_retains_disposer_until_explicit_retry(self):
        seen, contexts = [], []
        def mount(context, config):
            contexts.append(context)
            def dispose():
                seen.append('attempt')
                if len(seen) == 1:
                    raise ValueError('retry explicitly')
            context.defer(dispose)
        host = PluginHost({'version': 1, 'plugins': [{'id': 'p', 'use': 'p'}]},
                          [PluginSpec('p', (), (), mount)])
        self.addCleanup(host.close)
        with self.assertRaises(BaseExceptionGroup):
            host.close()
        self.assertEqual(len(contexts[0]._resources), 1)
        self.assertIsNone(host._pump_owner)
        host.close()
        self.assertEqual(contexts[0]._resources, {})
        self.assertEqual(seen, ['attempt', 'attempt'])
        snapshot = host.inspect()
        host.close()
        self.assertEqual(host.inspect(), snapshot)
        snapshot['state'] = 'forged'
        self.assertEqual(host.inspect()['state'], 'disposed')

    def test_unsealed_context_rejects_recursive_lifecycle_but_pump_recovers(self):
        failures = []
        def mount(context, config):
            try:
                context.host.close()
            except PluginError as exc:
                failures.append(str(exc))
        host = PluginHost({'version': 1, 'plugins': [{'id': 'p', 'use': 'p'}]},
                          [PluginSpec('p', (), (), mount)])
        self.addCleanup(host.close)
        self.assertEqual(len(failures), 1)
        self.assertIn('PLUGIN_LIFECYCLE_BUSY', failures[0])
        self.assertIsNone(host._pump_owner)
        self.assertEqual(host.inspect()['state'], 'ready')


class PythonHostOwnershipLintContracts(unittest.TestCase):
    def test_reviewed_guards_and_registered_models_are_mandatory(self):
        import json
        from pathlib import Path
        from .formal_python_host import ownership_violations
        root = Path(__file__).resolve().parents[2]
        self.assertEqual([], ownership_violations(root))
        registry = json.loads((root / 'formal/registry-v1.json').read_text('utf-8'))
        owner = next(m for m in registry['machines'] if m['id'] == 'python_host_lifecycles')
        self.assertEqual(len(owner['models']), 4)
        self.assertEqual(len(owner['negative_controls']), 20)
        self.assertEqual({Path(m['module']).stem for m in owner['models']},
                         {'PythonLease', 'PythonPump', 'PythonConfiguration', 'HttpLifespan'})

    def test_hash_refresh_cannot_hide_local_ownership_mutations(self):
        from copy import deepcopy
        import json
        from pathlib import Path
        import shutil
        import tempfile
        from .formal_state_machines import registry_violations, source_digest
        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / 'formal/registry-v1.json').read_text('utf-8'))
        registry['machines'] = [m for m in registry['machines'] if m['id'] == 'python_host_lifecycles']
        registry['discovery'] = {'roots': [], 'exclusions': []}
        plugin = 'kb_platform/plugin_host.py'
        http = 'kb_platform/host_applications.py'
        mutations = [
            (plugin, 'if root._pump_owner is not None or root._final_snapshot is not None:',
             'if root._pump_owner is None or root._final_snapshot is not None:'),
            (plugin, 'if root._pump_owner is owner:', 'if root._pump_owner is not owner:'),
            (plugin, 'settled = not authority.users', 'settled = True'),
            (plugin, 'authority.users -= 1', 'authority.users -= 0'),
            (plugin, "self._cancellations.pop(action['operation'], None)", 'pass'),
            (plugin, 'timer.join()', 'pass'),
            (plugin, 'if self._entries.get(plugin) is not entry:', 'if False:'),
            (plugin, "self._final_snapshot = self._collection['snapshot']", 'self._final_snapshot = {}'),
            (plugin, 'self.host._release(lease)', 'pass'),
            (plugin, 'self._release(authority)', 'pass'),
            (plugin, 'if token is not None:', 'if token is None:'),
            (http, 'self._finished = self._entered', 'self._finished = False'),
            (http, 'if not self._finished:', 'if True:'),
            (http, 'self._entered = True', 'self._entered = False'),
            (http, 'router._shutdown()', 'router.shutdown()'),
            ('formal/python_host_lifecycles/PythonLease.tla', 'nestedSeen /\\ ~failedSetup', '~failedSetup'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copytree(root / 'formal/controls', target / 'formal/controls')
            shutil.copytree(root / 'formal/python_host_lifecycles', target / 'formal/python_host_lifecycles')
            sources = {b['path'] for m in registry['machines'] for b in m['source_bindings']}
            sources |= {p for m in registry['machines'] for p in m['owners']}
            sources.add('kb_platform/rag_testing/foundation_integration.py')
            for path in sources:
                destination = target / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / path, destination)
            for path, old, new in mutations:
                with self.subTest(guard=old):
                    original = (root / path).read_text('utf-8')
                    self.assertIn(old, original)
                    (target / path).write_text(original.replace(old, new), encoding='utf-8')
                    refreshed = deepcopy(registry)
                    for machine in refreshed['machines']:
                        for binding in machine['source_bindings']:
                            if binding['path'] == path:
                                binding['sha256'] = source_digest(target, binding)
                    errors = registry_violations(target, refreshed)
                    self.assertTrue(any('python host ownership:' in error for error in errors), errors)
                    self.assertFalse(any('source review drift' in error for error in errors), errors)
                    (target / path).write_text(original, encoding='utf-8')
