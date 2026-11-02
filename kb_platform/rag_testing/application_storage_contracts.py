from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from kb_platform.application_storage import ApplicationStorage, StorageClient
from kb_platform.storage_catalog import initialize_configuration
from kb_platform.storage_object_wire import CHUNK_BYTES, ObjectTransferEndpoint
from kb_platform.storage_process import encode_value
from kb_platform.storage_sdk import StorageError


@contextmanager
def objects():
    with TemporaryDirectory() as directory:
        configuration = initialize_configuration({'version': 2, 'application_id': 'storage-wire-contracts',
            'instances': {'originals': {'provider': 'files', 'config': {'root': str(Path(directory) / 'objects')}}},
            'services': {'objects': {'instance': 'originals', 'export': 'object'}},
            'bindings': {'originals': {'objects': {'service': 'objects', 'owner': 'knowledge', 'space': 'originals'}}}})
        service = ApplicationStorage(configuration)
        try:
            yield service
        finally:
            service.close()


class JsonDependency:
    def __init__(self, service):
        self.service, self.messages = service, []

    def __getattr__(self, method):
        def call(request):
            encoded = json.dumps(request, allow_nan=False)
            self.messages.append((method, encoded))
            result = getattr(self.service, method)(json.loads(encoded))
            return json.loads(json.dumps(result, allow_nan=False))
        return call


class ApplicationStorageContracts(unittest.TestCase):
    def test_large_object_roundtrip_uses_bounded_json_chunks_and_keeps_tenant_isolation(self):
        with objects() as service:
            dependency = JsonDependency(service)
            client = StorageClient(dependency, 'originals.objects')
            data = b'\x00\xffsynthetic-upload\n' * 90000
            reference = client.put('tenant-a', data)
            self.assertEqual(reference.sha256, sha256(data).hexdigest())
            self.assertEqual(client.read('tenant-a', reference.locator, sha256=reference.sha256), data)
            self.assertEqual(client.stat('tenant-a', reference.locator), reference)
            self.assertLess(max(len(encoded.encode()) for _, encoded in dependency.messages), 100000)
            self.assertGreater(sum(json.loads(encoded).get('request', {}).get('op') == 'write'
                                   for _, encoded in dependency.messages), 1)
            with self.assertRaisesRegex(ValueError, 'BLOB_ACCESS_DENIED'):
                client.read('tenant-b', reference.locator)
            self.assertEqual(service._objects['originals.objects']._handles, {})

    def test_empty_object_roundtrip_and_corrupt_or_incomplete_upload_never_commit(self):
        with objects() as service:
            client = StorageClient(JsonDependency(service), 'originals.objects')
            ref = client.put('tenant', b'')
            self.assertEqual(client.read('tenant', ref.locator), b'')
        store = Mock()
        endpoint = ObjectTransferEndpoint(store)
        for data in (b'', b'bad'):
            opened = endpoint.execute({'op': 'begin_upload', 'tenant_id': 'tenant',
                'size': 3, 'sha256': sha256(b'yes').hexdigest()})
            if data:
                endpoint.execute({'op': 'write', 'handle': opened['handle'], 'offset': 0,
                                  'data': encode_value(data)})
            with self.assertRaises(StorageError):
                endpoint.execute({'op': 'finish_upload', 'handle': opened['handle']})
            self.assertEqual(endpoint._handles, {})
        store.put.assert_not_called()
        endpoint.close()

    def test_interrupted_upload_is_cancelled_without_replaying_write(self):
        with objects() as service:
            dependency = JsonDependency(service)
            client = StorageClient(dependency, 'originals.objects')
            original = service.objects
            writes = []
            def fail(request):
                if request['request']['op'] == 'write':
                    writes.append(request)
                    raise StorageError('SYNTHETIC_DISCONNECT')
                return original(request)
            with patch.object(service, 'objects', side_effect=fail):
                with self.assertRaisesRegex(StorageError, 'SYNTHETIC_DISCONNECT'):
                    client.put('tenant', b'x' * (CHUNK_BYTES * 2))
            self.assertEqual(len(writes), 1)
            self.assertEqual(service._objects['originals.objects']._handles, {})

    def test_close_releases_unfinished_transfers_and_rejects_new_calls(self):
        with objects() as service:
            opened = service.objects({'binding': 'originals.objects', 'request': {
                'op': 'begin_upload', 'tenant_id': 'tenant', 'size': 3, 'sha256': sha256(b'yes').hexdigest()}})
            endpoint = service._objects['originals.objects']
            stream = endpoint._handles[opened['handle']].stream
            service.close()
            self.assertTrue(stream.closed)
            self.assertEqual(endpoint._handles, {})
            with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_UNAVAILABLE'):
                service.describe({'binding': 'originals.objects'})

    def test_concurrent_lazy_creation_constructs_one_endpoint(self):
        with objects() as service:
            with patch('kb_platform.storage_object_wire.ObjectTransferEndpoint', wraps=ObjectTransferEndpoint) as factory:
                def begin(index):
                    return service.objects({'binding': 'originals.objects', 'request': {
                        'op': 'begin_upload', 'tenant_id': 'tenant', 'size': 0, 'sha256': sha256(b'').hexdigest()}})
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = tuple(pool.map(begin, range(8)))
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(len({row['handle'] for row in results}), 8)

    def test_close_refuses_active_operation_and_keeps_failed_cleanup_for_retry(self):
        with objects() as service:
            entered, release = Event(), Event()
            def busy():
                with service._operation():
                    entered.set()
                    self.assertTrue(release.wait(3))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(busy)
                self.assertTrue(entered.wait(3))
                try:
                    with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_BUSY'):
                        service.close()
                finally:
                    release.set()
                future.result(3)
            endpoint = Mock()
            endpoint.close.side_effect = [StorageError('SYNTHETIC_CLOSE_FAILURE'), None]
            service._structured['pending'] = endpoint
            with self.assertRaisesRegex(StorageError, 'SYNTHETIC_CLOSE_FAILURE'):
                service.close()
            self.assertIs(service._structured['pending'], endpoint)
            self.assertTrue(service._instances)
            service.close()
            self.assertEqual(endpoint.close.call_count, 2)
            self.assertEqual(service._instances, [])

    def test_transfer_handle_limits_offsets_and_expiry(self):
        endpoint = ObjectTransferEndpoint(Mock())
        with patch('kb_platform.storage_object_wire.monotonic', return_value=0):
            handles = [endpoint.execute({'op': 'begin_upload', 'tenant_id': 'tenant', 'size': 1,
                       'sha256': sha256(b'a').hexdigest()})['handle'] for _ in range(16)]
            with self.assertRaisesRegex(StorageError, 'STORAGE_OBJECT_TRANSFER_LIMIT'):
                endpoint.execute({'op': 'begin_upload', 'tenant_id': 'tenant', 'size': 1,
                                  'sha256': sha256(b'a').hexdigest()})
            with self.assertRaisesRegex(StorageError, 'STORAGE_OBJECT_TRANSFER_OFFSET_INVALID'):
                endpoint.execute({'op': 'write', 'handle': handles[0], 'offset': 1, 'data': encode_value(b'a')})
            stream = endpoint._handles[handles[0]].stream
        with patch('kb_platform.storage_object_wire.monotonic', return_value=301):
            with self.assertRaisesRegex(StorageError, 'STORAGE_OBJECT_TRANSFER_HANDLE_INVALID'):
                endpoint.execute({'op': 'finish_upload', 'handle': handles[0]})
        self.assertTrue(stream.closed)
        self.assertEqual(endpoint._handles, {})
        endpoint.close()
