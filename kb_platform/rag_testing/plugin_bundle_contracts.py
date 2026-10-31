from copy import deepcopy
import unittest

from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError
from kb_platform.plugin_profiles import expand_profile
from kb_platform.knowledge_profile import load_knowledge_profile
from kb_platform.harness_profile import load_agent_profile


class BundleContracts(unittest.TestCase):
    def test_expansion_is_detached_and_explicit_overrides_replace_config(self):
        bundle = {'version': 1, 'plugins': [
            {'id': 'service', 'use': 'one', 'config': {'a': 1, 'b': 2}}]}
        original = deepcopy(bundle)
        selected = expand_profile({'version': 1, 'bundles': ['base'], 'plugins': [
            {'id': 'service', 'use': 'two', 'config': {'a': 3}}]}, {'base': bundle})
        self.assertEqual(selected['plugins'], [{'id': 'service', 'use': 'two', 'config': {'a': 3}}])
        selected['plugins'][0]['config']['a'] = 5
        self.assertEqual(bundle, original)

    def test_cycles_unknown_bundles_and_implicit_collisions_fail(self):
        cases = [
            ({'version': 1, 'bundles': ['missing']}, {}, 'UNKNOWN_PLUGIN_BUNDLE'),
            ({'version': 1, 'bundles': ['a']}, {'a': {'version': 1, 'bundles': ['a']}},
             'CYCLIC_PLUGIN_BUNDLE'),
            ({'version': 1, 'bundles': ['a', 'a']}, {'a': {'version': 1}}, 'DUPLICATE_PLUGIN_BUNDLE'),
            ({'version': 1, 'bundles': ['a', 'b']},
             {key: {'version': 1, 'plugins': [{'id': 'x', 'use': 'one'}]} for key in ['a', 'b']},
             'DUPLICATE_BUNDLE_PLUGIN_ID'),
        ]
        for profile, catalog, code in cases:
            with self.subTest(code=code), self.assertRaises(PluginError) as raised:
                expand_profile(profile, catalog)
            self.assertEqual(raised.exception.code, code)

    def test_disabling_required_plugin_rejects_before_mount(self):
        mounted = []
        profile = expand_profile({'version': 1, 'bundles': ['base'],
                                  'plugins': [{'id': 'store', 'disabled': True}]}, {
            'base': {'version': 1, 'plugins': [{'id': 'store', 'use': 'store'},
                                              {'id': 'app', 'use': 'app'}]}})
        catalog = [PluginSpec('store', ('store',), (), lambda ctx, config: mounted.append('store')),
                   PluginSpec('app', ('app',), ('store',), lambda ctx, config: mounted.append('app'))]
        with self.assertRaises(PluginError):
            PluginHost(profile, catalog)
        self.assertEqual(mounted, [])

    def test_product_default_does_not_mount_optional_core(self):
        knowledge = load_knowledge_profile(environment={})
        core = load_agent_profile(environment={})
        product_uses = {row['use'] for row in knowledge['plugins']}
        core_uses = {row['use'] for row in core['plugins']}
        self.assertIn('knowledge.driver.graph', product_uses)
        self.assertNotIn('harness.loop.model-tools', product_uses)
        self.assertNotIn('harness.sessions.memory', product_uses)
        self.assertIn('harness.sessions.durable', core_uses)
        self.assertFalse(any(key.startswith('knowledge.') for key in core_uses))

    def test_nested_bundle_and_partial_override_preserve_service_choice(self):
        selected = expand_profile({'version': 1, 'bundles': ['outer'],
                                   'plugins': [{'id': 'a', 'config': {'value': 2}}]}, {
            'outer': {'version': 1, 'bundles': ['inner']},
            'inner': {'version': 1, 'plugins': [{'id': 'a', 'use': 'service', 'config': {'value': 1}}]}})
        self.assertEqual(selected['plugins'], [{'id': 'a', 'use': 'service', 'config': {'value': 2}}])

    def test_store_profile_applies_explicit_path_and_environment_override(self):
        original = {'version': 1, 'plugins': [
            {'id': 'store', 'use': 'harness.store.sqlite', 'config': {'root': 'session-path'}},
            {'id': 'knowledge-store', 'use': 'knowledge.sessions'},
            {'id': 'workflow', 'use': 'knowledge.workflow.graph'}]}
        before = deepcopy(original)
        selected = load_knowledge_profile(original, environment={})
        self.assertEqual(original, before)
        self.assertEqual(selected['plugins'][0]['config']['root'], 'session-path')
        self.assertEqual([row['use'] for row in selected['plugins']],
                         ['harness.store.sqlite', 'knowledge.sessions', 'knowledge.workflow.graph'])
        overridden = load_knowledge_profile(original, environment={'KB_AGENT_ROOT': 'override'})
        self.assertEqual(overridden['plugins'][0]['config']['root'], 'override')
