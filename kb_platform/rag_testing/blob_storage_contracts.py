from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.blob_migration import migrate_legacy_originals
from kb_platform.blob_service import BlobRef, BlobService
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.storage import FileBlobProvider, OriginalStore, S3BlobProvider
from kb_platform.storage_catalog import default_storage_providers, initialize_configuration
from kb_platform.storage_plugins import compile_storage_plugins, require_storage
from kb_platform.storage_sdk import OBJECT_V1, StorageSlot


class BlobStorageContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def store(self):
        value = OriginalStore(self.root)
        self.addCleanup(value.close)
        return value

    def test_repeated_upload_restart_and_backend_independent_reference(self):
        store = self.store()
        locator, checksum = store.put('tenant-a', b'original bytes')
        self.assertEqual((locator, checksum), store.put('tenant-a', b'original bytes'))
        self.assertTrue(locator.startswith('blob:v1:'))
        self.assertNotIn(str(self.root), locator)
        store.close()
        restarted = self.store()
        self.assertEqual(restarted.read(locator, 'tenant-a', checksum), b'original bytes')
        self.assertEqual(restarted.stat(locator, 'tenant-a', checksum).size, 14)

    def test_tenant_and_plugin_spaces_reject_foreign_references(self):
        service = BlobService()
        self.addCleanup(service.close)
        service.register('shared', FileBlobProvider(self.root))
        first, second = (service.bind('shared', space=space) for space in ('project-a/tasks', 'project-b/tasks'))
        ref = first.put('tenant', b'private')
        self.assertNotEqual(ref.locator, second.put('tenant', b'private').locator)
        with self.assertRaisesRegex(ValueError, 'BLOB_ACCESS_DENIED'):
            second.read('tenant', ref.locator)
        with self.assertRaisesRegex(ValueError, 'BLOB_ACCESS_DENIED'):
            first.read('other-tenant', ref.locator)

    def test_tamper_cannot_be_healed_by_repeated_upload(self):
        store = self.store()
        locator, checksum = store.put('tenant', b'original')
        path = store.checked_path(locator, 'tenant', checksum)
        path.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            store.read(locator, 'tenant', checksum)
        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            store.put('tenant', b'original')
        self.assertEqual(path.read_bytes(), b'tampered')
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_missing_object_and_malformed_reference_fail_explicitly(self):
        store = self.store()
        locator, checksum = store.put('tenant', b'data')
        store.checked_path(locator, 'tenant', checksum).unlink()
        with self.assertRaises(FileNotFoundError):
            store.read(locator, 'tenant', checksum)
        for invalid in ('../../secret', locator + ':x', locator[:-1] + '01', locator.replace(':4', ':-1')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                store.read(invalid, 'tenant', checksum)
        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            store.read(locator, 'tenant', '0' * 64)

    def test_file_backend_rejects_provider_object_path_escape(self):
        provider = FileBlobProvider(self.root)
        self.addCleanup(provider.close)
        with self.assertRaisesRegex(ValueError, 'BLOB_SPACE_ESCAPE'):
            provider.path(SimpleNamespace(object_key='../outside'))

    def test_retirement_pins_inflight_read_and_stale_binding_cannot_rebind(self):
        entered, proceed, failures = Event(), Event(), []
        class Blocking(FileBlobProvider):
            def read(self, ref):
                entered.set()
                if not proceed.wait(5):
                    raise AssertionError('read release timeout')
                return super().read(ref)
        service = BlobService()
        self.addCleanup(service.close)
        provider = Blocking(self.root)
        remove = service.register('route', provider)
        bound = service.bind('route', space='project')
        ref = bound.put('tenant', b'data')
        def read():
            try:
                self.assertEqual(bound.read('tenant', ref.locator), b'data')
            except BaseException as error:
                failures.append(error)
        thread = Thread(target=read)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, 'BLOB_BUSY'):
                remove()
            self.assertFalse(provider.closed)
            with self.assertRaisesRegex(ValueError, 'BLOB_ROUTE_UNAVAILABLE'):
                bound.read('tenant', ref.locator)
        finally:
            proceed.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        remove()
        self.assertTrue(provider.closed)
        service.register('route', FileBlobProvider(self.root))
        with self.assertRaisesRegex(ValueError, 'BLOB_BINDING_CHANGED'):
            bound.read('tenant', ref.locator)

    def test_failed_cleanup_keeps_registration_for_retry(self):
        class FailingClose(FileBlobProvider):
            def close(self):
                if not self.closed:
                    self.closed = True
                    raise OSError('fixture close failure')
        service = BlobService()
        remove = service.register('route', FailingClose(self.root))
        with self.assertRaisesRegex(OSError, 'fixture close failure'):
            remove()
        self.assertEqual(service.inspect()['route']['phase'], 'cleanup_failed')
        with self.assertRaisesRegex(ValueError, 'BLOB_REGISTRATION_UNAVAILABLE'):
            service.register('route', FileBlobProvider(self.root))
        remove()
        service.close()

    def test_host_registry_provider_binding_and_independent_consumer(self):
        def mount(ctx, config):
            ctx.provide('tasks', require_storage(ctx, 'objects'))
        spec = PluginSpec('tasks', ('tasks',), (StorageSlot('objects', OBJECT_V1),), mount)
        providers = default_storage_providers()
        configuration = initialize_configuration({
            'version': 2, 'application_id': 'project-a',
            'instances': {'files': {'provider': 'files', 'config': {'root': str(self.root)}}},
            'services': {'files.objects': {'instance': 'files', 'export': 'object'}},
            'bindings': {'tasks': {'objects': {
                'service': 'files.objects', 'owner': 'tasks', 'space': 'originals'}}},
        }, provider_catalog=providers)
        profile, specs = compile_storage_plugins(
            {'version': 1, 'plugins': [{'id': 'tasks', 'use': 'tasks'}]}, (spec,), configuration,
            provider_catalog=providers)
        host = PluginHost(profile, specs)
        self.addCleanup(host.close)
        ref = host.call('tasks', 'put', 'tenant', b'task bytes')
        self.assertEqual(host.call('tasks', 'read', 'tenant', ref.locator), b'task bytes')
        self.assertEqual(host.service('tasks').identity.dataset_id,
                         configuration['bindings']['tasks']['objects']['dataset_id'])

    def test_two_project_consumers_share_provider_and_isolate_blob_spaces(self):
        def mount(ctx, config):
            ctx.provide(ctx.plugin_id, require_storage(ctx, 'objects'))

        names = ('project-a', 'project-b')
        specs = tuple(PluginSpec(name, (name,), (StorageSlot('objects', OBJECT_V1),), mount) for name in names)
        providers = default_storage_providers()
        configuration = initialize_configuration({
            'version': 2, 'application_id': 'shared-host',
            'instances': {'files': {'provider': 'files', 'config': {'root': str(self.root)}}},
            'services': {'files.objects': {'instance': 'files', 'export': 'object'}},
            'bindings': {name: {'objects': {'service': 'files.objects', 'application_id': name,
                'owner': 'tasks', 'space': 'originals'}} for name in names},
        }, provider_catalog=providers)
        profile, catalog = compile_storage_plugins(
            {'version': 1, 'plugins': [{'id': name, 'use': name} for name in names]}, specs, configuration,
            provider_catalog=providers)
        with PluginHost(profile, catalog) as root:
            self.assertEqual(set(root.service('storage.registry').catalog()), {'files.objects'})
            first = root.call('project-a', 'put', 'tenant', b'private')
            second = root.call('project-b', 'put', 'tenant', b'private')
            self.assertNotEqual(first.locator, second.locator)
            with self.assertRaisesRegex(ValueError, 'BLOB_ACCESS_DENIED'):
                root.call('project-b', 'read', 'tenant', first.locator)
            self.assertEqual(root.call('project-a', 'read', 'tenant', first.locator), b'private')
            self.assertEqual(root.call('project-b', 'read', 'tenant', second.locator), b'private')

    def test_explicit_legacy_migration_preserves_sources_and_emits_verified_mapping(self):
        tenant, data = 'legacy-tenant', b'legacy original'
        checksum = sha256(data).hexdigest()
        legacy = self.root / 'legacy'
        path = legacy / sha256(tenant.encode()).hexdigest() / checksum[:2] / checksum
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        records = [{'tenant_id': tenant, 'original_path': str(path), 'original_sha256': checksum}]
        target = OriginalStore(self.root / 'destination')
        self.addCleanup(target.close)
        output = self.root / 'audit'
        results = migrate_legacy_originals(records, legacy_root=legacy, target=target, output=output)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(target.read(results[0]['blob_locator'], tenant, checksum), data)
        self.assertEqual(json.loads((output / 'mapping.json').read_text())['records'], results)
        self.assertFalse(json.loads((output / 'summary.json').read_text())['database_updated'])
        with self.assertRaises(FileExistsError):
            migrate_legacy_originals(records, legacy_root=legacy, target=target, output=output)

    def test_legacy_plan_rejects_foreign_tenant_and_corruption_before_copy(self):
        data, tenant = b'legacy original', 'owner'
        checksum = sha256(data).hexdigest()
        path = self.root / sha256(tenant.encode()).hexdigest() / checksum[:2] / checksum
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        target = OriginalStore(self.root / 'destination')
        self.addCleanup(target.close)
        for scenario in ('tenant', 'corruption'):
            record = {'tenant_id': 'intruder' if scenario == 'tenant' else tenant,
                      'original_path': str(path), 'original_sha256': checksum}
            if scenario == 'corruption':
                path.write_bytes(b'corrupt')
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                migrate_legacy_originals([record], legacy_root=self.root, target=target,
                                         output=self.root / ('audit-' + scenario))
            self.assertEqual(list((self.root / 'destination').rglob('*')), [])


class S3WireFixture:
    def __init__(self, *, status=200, redirect=None):
        self.calls, self.objects = [], {}
        self.status, self.redirect = status, redirect

    def send(self, request):
        from botocore.awsrequest import AWSResponse
        from urllib3.response import HTTPResponse

        self.calls.append((request.method, request.url))
        def response(code, data=b'', **headers):
            return AWSResponse(request.url, code, {'Content-Length': str(len(data)), **headers},
                HTTPResponse(body=BytesIO(data), preload_content=False))
        if self.status != 200:
            return response(self.status, b'<Error><Code>FixtureFailure</Code></Error>',
                **({'Location': self.redirect, 'x-amz-bucket-region': 'other-region'} if self.redirect else {}))
        if request.method == 'PUT':
            if request.url in self.objects:
                return response(412, b'<Error><Code>PreconditionFailed</Code></Error>')
            if request.headers.get('If-None-Match') != b'*':
                return response(400)
            body = request.body.read() if hasattr(request.body, 'read') else request.body
            self.objects[request.url] = (body, request.headers['x-amz-meta-sha256'].decode())
            return response(200)
        if request.url not in self.objects:
            return response(404, b'<Error><Code>NoSuchKey</Code></Error>')
        data, checksum = self.objects[request.url]
        return response(200, data, **{'x-amz-meta-sha256': checksum})


class S3TransportContracts(unittest.TestCase):
    def provider(self, transport):
        provider = S3BlobProvider(endpoint_url='https://synthetic.invalid', bucket='fixture',
                                  access_key='synthetic', secret_key='synthetic')
        self.addCleanup(provider.close)
        patched = patch.object(provider.client._endpoint.http_session, 'send', side_effect=transport.send)
        patched.start()
        self.addCleanup(patched.stop)
        return provider

    def test_conditional_put_roundtrip_missing_object_and_content_tamper(self):
        transport, service = S3WireFixture(), BlobService()
        self.addCleanup(service.close)
        service.register('s3', self.provider(transport))
        bound = service.bind('s3', space='project-a/originals')
        ref = bound.put('tenant', b'original data')
        self.assertEqual(bound.put('tenant', b'original data'), ref)
        self.assertEqual(bound.read('tenant', ref.locator), b'original data')
        self.assertEqual(bound.stat('tenant', ref.locator), ref)
        key = next(iter(transport.objects))
        transport.objects[key] = (b'corrupt bytes', ref.sha256)
        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            bound.read('tenant', ref.locator)
        del transport.objects[key]
        with self.assertRaises(FileNotFoundError):
            bound.read('tenant', ref.locator)
        self.assertEqual(sum(method == 'PUT' for method, path in transport.calls), 2)

    def test_write_errors_and_redirects_are_never_retried(self):
        for code in (301, 307, 500, 503):
            with self.subTest(code=code):
                transport = S3WireFixture(status=code, redirect='https://redirect.invalid')
                provider = self.provider(transport)
                ref = BlobRef('1' * 64, '2' * 64, sha256(b'data').hexdigest(), 4)
                with self.assertRaises(OSError):
                    provider.put_if_absent(ref, b'data')
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(transport.calls[0][1].startswith('https://synthetic.invalid/'))


if __name__ == '__main__':
    unittest.main()
