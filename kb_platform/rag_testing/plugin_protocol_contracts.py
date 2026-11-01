from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.plugin_host import PLUGIN_PROTOCOL, PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


def profile(config=None):
    return {'version': 1, 'plugins': [{'id': 'fixture', 'use': 'fixture', 'config': config or {}}]}


def empty_mount(context, config):
    return None


class PluginProtocolContracts(unittest.TestCase):
    def test_protocol_version_and_declarations_are_checked_before_mount(self):
        spec = PluginSpec('fixture', (), (), empty_mount)
        self.assertEqual(spec.protocol, PLUGIN_PROTOCOL)
        for change, code in (
            ({'protocol': 'evidence-plugin/2'}, 'UNSUPPORTED_PLUGIN_PROTOCOL'),
            ({'key': ''}, 'INVALID_PLUGIN_SPEC'),
            ({'version': ''}, 'INVALID_PLUGIN_SPEC'),
            ({'provides': 'service'}, 'INVALID_PLUGIN_SERVICES'),
            ({'requires': ('service', 'service')}, 'INVALID_PLUGIN_SERVICES'),
            ({'concurrency': 'unbounded'}, 'INVALID_PLUGIN_SPEC'),
        ):
            with self.subTest(change=change), self.assertRaises(PluginError) as failure:
                replace(spec, **change)
            self.assertEqual(failure.exception.code, code)

    def test_all_catalog_entries_must_be_specs_before_runtime_is_created(self):
        with patch('kb_platform.plugin_host.native_call') as native:
            with self.assertRaisesRegex(PluginError, 'INVALID_PLUGIN_CATALOG'):
                PluginHost(profile(), [SimpleNamespace(key='fixture')])
            native.assert_not_called()

    def test_mount_and_configuration_hooks_must_accept_the_synchronous_protocol(self):
        async def asynchronous(*args):
            return None
        def generator(*args):
            yield None
        class AsyncCallable:
            async def __call__(self, *args):
                return None
        for mount in (None, lambda: None, lambda context, config, extra: None,
                      asynchronous, generator, AsyncCallable()):
            with self.subTest(mount=mount), self.assertRaises(PluginError) as failure:
                PluginSpec('fixture', (), (), mount)
            self.assertEqual(failure.exception.code, 'INVALID_PLUGIN_MOUNT')
        for hook in (lambda: None, asynchronous, generator):
            for field in ('config_check', 'config_inputs'):
                with self.subTest(field=field), self.assertRaises(PluginError) as failure:
                    PluginSpec('fixture', (), (), empty_mount, **{field: hook})
                self.assertEqual(failure.exception.code, 'INVALID_PLUGIN_CONFIG_HOOK')

    def test_missing_schema_means_empty_configuration_and_invalid_update_preserves_registration(self):
        calls = []
        def mount(context, config):
            calls.append(config)
            context.provide('optional', None)
        spec = PluginSpec('fixture', ('optional',), (), mount)
        with self.assertRaises(PluginError) as failure:
            PluginHost(profile({'unknown': True}), [spec])
        self.assertEqual(failure.exception.code, 'INVALID_PLUGIN_CONFIG')
        self.assertEqual(calls, [])
        with PluginHost(profile(), [spec]) as host:
            before = host.inspect()
            with self.assertRaises(PluginError):
                host.update('fixture', config={'unknown': True})
            self.assertEqual(host.inspect()['instances'], before['instances'])
            self.assertEqual(host.inspect()['desired_revision'], before['desired_revision'])
            self.assertIsNone(host.service('optional'))
        self.assertEqual(calls, [{}])

    def test_explicit_schema_is_copied_and_checks_configuration_before_mount(self):
        schema = {'type': 'object', 'properties': {'count': {'type': 'integer'}},
                  'additionalProperties': False, 'required': ['count']}
        spec = PluginSpec('fixture', (), (), empty_mount, config_schema=schema)
        schema['properties']['count']['type'] = 'string'
        with PluginHost(profile({'count': 1}), [spec]):
            pass
        with self.assertRaises(PluginError):
            PluginHost(profile({'count': 1.0}), [spec])
        for schema in (True, {}, {'type': 'array'}):
            with self.subTest(schema=schema), self.assertRaises(PluginError):
                replace(spec, config_schema=schema)

    def test_deferred_awaitable_mount_is_rejected_and_partial_resources_are_cleaned(self):
        calls = []
        async def pending():
            calls.append('must not run')
        def mount(context, config):
            context.defer(lambda: calls.append('released'))
            context.provide('value', object())
            return pending()
        with self.assertRaises(PluginError) as failure:
            PluginHost(profile(), [PluginSpec('fixture', ('value',), (), mount)])
        self.assertEqual(failure.exception.code, 'INVALID_PLUGIN_MOUNT_RESULT')
        self.assertEqual(calls, ['released'])

    def test_deferred_awaitable_config_hook_is_rejected_before_runtime_is_created(self):
        async def pending():
            return {}
        for hook in ('config_check', 'config_inputs'):
            spec = PluginSpec('fixture', (), (), empty_mount, **{hook: lambda config: pending()})
            with self.subTest(hook=hook), patch('kb_platform.plugin_host.native_call') as native:
                with self.assertRaisesRegex(PluginError, 'INVALID_PLUGIN_CONFIG_HOOK_RESULT'):
                    PluginHost(profile(), [spec])
                native.assert_not_called()

    def test_asynchronous_disposal_cannot_report_success_or_forget_owned_resources(self):
        pending = [True]
        released = []
        async def asynchronous():
            released.append('must not run')
        def cleanup():
            if pending[0]:
                return asynchronous()
            released.append('released')
        def mount(context, config):
            with self.assertRaisesRegex(PluginError, 'INVALID_PLUGIN_DISPOSER'):
                context.defer(asynchronous)
            context.defer(cleanup)
        host = PluginHost(profile(), [PluginSpec('fixture', (), (), mount)])
        try:
            with self.assertRaises(ExceptionGroup):
                host.close()
            self.assertEqual(host.inspect()['state'], 'cleanup_failed')
            self.assertEqual(released, [])
        finally:
            pending[0] = False
            host.close()
        self.assertEqual(released, ['released'])
        self.assertEqual(host.inspect()['state'], 'disposed')
