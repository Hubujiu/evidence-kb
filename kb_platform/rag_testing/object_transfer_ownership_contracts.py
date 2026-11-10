from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from kb_platform.storage_adapters.objects import file_objects
from kb_platform.storage_object_wire import CHUNK_BYTES, ObjectTransferClient, ObjectTransferEndpoint
from kb_platform.storage_process import decode_value, encode_value
from kb_platform.storage_sdk import ExistingDataset, SpaceKey, StorageError
from .formal_state_machines import python_symbols, source_digest


class ObjectTransferOwnershipContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = file_objects({'root': str(self.root)})
        self.addCleanup(self.provider.close)
        space = SpaceKey('object-transfer-contract', 'objects', 'originals')
        identity = self.provider.initialize('object', space)
        self.store = self.provider.open_space(space, ExistingDataset(identity.dataset_id))
        self.endpoint = ObjectTransferEndpoint(self.store)
        self.addCleanup(self.endpoint.close)
        self.calls = []
        self.streams = []

    def call(self, request):
        self.calls.append(request['op'])
        result = self.endpoint.execute(request)
        if request['op'].startswith('begin_'):
            self.streams.append(self.endpoint._handles[result['handle']].stream)
        return result

    def begin(self, data=b'ab', digest=None):
        return self.call({'op': 'begin_upload', 'tenant_id': 'tenant', 'size': len(data),
                          'sha256': digest or sha256(data).hexdigest()})['handle']

    def write(self, handle, data=b'ab', offset=0):
        return self.call({'op': 'write', 'handle': handle, 'offset': offset, 'data': encode_value(data)})

    def objects(self):
        return [path for path in self.root.rglob('*') if path.is_file() and '.storage-identities' not in path.parts]

    def test_actual_file_store_empty_and_multichunk_roundtrips_release_all_handles(self):
        client = ObjectTransferClient(self.call)
        for data in (b'', b'\x00\xffbinary' * (CHUNK_BYTES // 4)):
            with self.subTest(size=len(data)):
                ref = client.put('tenant', data)
                self.assertEqual(self.store.read('tenant', ref.locator), data)
                self.assertEqual(client.read('tenant', ref.locator, sha256=ref.sha256), data)
                self.assertEqual(self.endpoint._handles, {})
                self.assertTrue(all(stream.closed for stream in self.streams))
        self.assertEqual(self.calls.count('finish_upload'), 2)
        self.assertEqual(self.calls.count('cancel'), 4)
        self.assertGreater(self.calls.count('write'), 1)
        self.assertEqual(len(self.objects()), 2)

    def test_incomplete_and_bad_digest_finish_discard_without_storage_effect(self):
        for data in (b'', b'xx'):
            handle = self.begin()
            if data:
                self.write(handle, data)
            with self.assertRaisesRegex(StorageError, 'INCOMPLETE|INTEGRITY_FAILURE'):
                self.call({'op': 'finish_upload', 'handle': handle})
            self.assertNotIn(handle, self.endpoint._handles)
            self.assertTrue(self.streams[-1].closed)
            with self.assertRaisesRegex(StorageError, 'HANDLE_INVALID'):
                self.call({'op': 'finish_upload', 'handle': handle})
        self.assertEqual(self.objects(), [])

    def test_storage_error_before_or_after_actual_put_never_replays_and_preserves_effect(self):
        original = self.store.put
        for effect in (False, True):
            self.calls.clear()
            written = []
            def fail(tenant, data):
                if effect:
                    written.append(original(tenant, data))
                raise OSError('controlled storage acknowledgment failure')
            with patch.object(self.store, 'put', side_effect=fail) as put:
                with self.assertRaisesRegex(OSError, 'acknowledgment failure'):
                    ObjectTransferClient(self.call).put('tenant', b'fault boundary')
                self.assertEqual(put.call_count, 1)
            self.assertEqual(self.calls.count('finish_upload'), 1)
            self.assertEqual(self.calls.count('cancel'), 1)
            self.assertEqual(self.endpoint._handles, {})
            self.assertTrue(self.streams[-1].closed)
            if effect:
                self.assertEqual(self.store.read('tenant', written[0].locator), b'fault boundary')
            else:
                self.assertEqual(self.objects(), [])

    def test_lost_finish_reply_does_not_replay_or_delete_committed_object(self):
        committed = []
        def lose_reply(request):
            result = self.call(request)
            if request['op'] == 'finish_upload':
                committed.append(decode_value(result['reference']))
                raise ConnectionError('controlled lost finish reply')
            return result
        with self.assertRaisesRegex(ConnectionError, 'lost finish reply'):
            ObjectTransferClient(lose_reply).put('tenant', b'committed')
        self.assertEqual(self.calls.count('finish_upload'), 1)
        self.assertEqual(self.calls.count('cancel'), 1)
        self.assertEqual(self.endpoint._handles, {})
        self.endpoint.close()
        self.assertEqual(self.store.read('tenant', committed[0].locator), b'committed')

    def test_bad_reference_after_real_put_discards_handle_without_undoing_storage(self):
        original, actual = self.store.put, []
        def corrupt_reference(tenant, data):
            actual.append(original(tenant, data))
            return replace(actual[-1], size=len(data) + 1)
        with patch.object(self.store, 'put', side_effect=corrupt_reference) as put:
            with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
                ObjectTransferClient(self.call).put('tenant', b'reference fault')
            self.assertEqual(put.call_count, 1)
        self.assertEqual(self.store.read('tenant', actual[0].locator), b'reference fault')
        self.assertEqual(self.endpoint._handles, {})
        self.assertTrue(self.streams[-1].closed)

    def test_wrong_write_ack_cancels_without_finish_or_replay(self):
        def wrong_ack(request):
            result = self.call(request)
            return {'offset': -1} if request['op'] == 'write' else result
        with self.assertRaisesRegex(StorageError, 'OFFSET_INVALID'):
            ObjectTransferClient(wrong_ack).put('tenant', b'body')
        self.assertEqual(self.calls, ['begin_upload', 'write', 'cancel'])
        self.assertEqual(self.endpoint._handles, {})
        self.assertEqual(self.objects(), [])

    def test_invalid_download_chunk_or_completion_is_cancelled_and_never_returned(self):
        data = b'a' * (CHUNK_BYTES + 1)
        ref = self.store.put('tenant', data)
        for mutation in ({'offset': True}, {'done': 1}, {'data': encode_value(b'')},
                         {'data': encode_value(b'changed'), 'offset': 7, 'done': True}):
            self.calls.clear()
            def corrupted(request):
                result = self.call(request)
                return {**result, **mutation} if request['op'] == 'read' else result
            with self.subTest(mutation=mutation), self.assertRaises((StorageError, ValueError)):
                ObjectTransferClient(corrupted).read('tenant', ref.locator)
            self.assertEqual(self.calls, ['begin_download', 'read', 'cancel'])
            self.assertEqual(self.endpoint._handles, {})
            self.assertTrue(self.streams[-1].closed)
        self.assertEqual(self.store.read('tenant', ref.locator), data)

    def test_download_auth_and_integrity_failure_never_allocate_a_handle(self):
        ref = self.store.put('tenant', b'original')
        for tenant, digest in (('other', ref.sha256), ('tenant', '0' * 64)):
            with self.subTest(tenant=tenant), self.assertRaises(ValueError):
                self.call({'op': 'begin_download', 'tenant_id': tenant,
                           'locator': ref.locator, 'sha256': digest})
            self.assertEqual(self.endpoint._handles, {})
        path = self.root.joinpath(*ref.object_key.split('/'))
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'BLOB_INTEGRITY_FAILURE'):
            self.call({'op': 'begin_download', 'tenant_id': 'tenant',
                       'locator': ref.locator, 'sha256': ref.sha256})
        self.assertEqual(self.endpoint._handles, {})
        self.assertEqual(self.streams, [])

    def test_failed_handle_publication_closes_real_spooled_stream(self):
        from tempfile import SpooledTemporaryFile
        stream = SpooledTemporaryFile(max_size=1, mode='w+b')
        self.addCleanup(stream.close)
        with patch('kb_platform.storage_object_wire.SpooledTemporaryFile', return_value=stream), \
                patch('kb_platform.storage_object_wire.monotonic', side_effect=OSError('controlled clock failure')):
            with self.assertRaisesRegex(OSError, 'clock failure'):
                self.begin()
        self.assertTrue(stream.closed)
        self.assertEqual(self.endpoint._handles, {})
        self.assertEqual(self.objects(), [])

    def test_cancel_transport_failure_does_not_replace_original_error_or_replay(self):
        def disconnected(request):
            if request['op'] == 'write':
                self.calls.append('write')
                raise ConnectionError('original upload disconnect')
            if request['op'] == 'cancel':
                self.calls.append('cancel')
                raise OSError('cleanup disconnect')
            return self.call(request)
        with self.assertRaisesRegex(ConnectionError, 'original upload disconnect'):
            ObjectTransferClient(disconnected).put('tenant', b'body')
        self.assertEqual(self.calls, ['begin_upload', 'write', 'cancel'])
        self.assertEqual(self.objects(), [])
        self.assertEqual(len(self.endpoint._handles), 1)
        self.endpoint.close()
        self.assertTrue(self.streams[0].closed)
        self.assertEqual(self.endpoint._handles, {})

    def test_idle_expiry_is_lazy_exact_and_invalid_mode_refreshes_touch(self):
        with patch('kb_platform.storage_object_wire.monotonic', return_value=0):
            first, refreshed = self.begin(), self.begin()
        with patch('kb_platform.storage_object_wire.monotonic', return_value=299):
            with self.assertRaisesRegex(StorageError, 'MODE_INVALID'):
                self.call({'op': 'read', 'handle': refreshed, 'offset': 0})
        self.assertIn(first, self.endpoint._handles)
        with patch('kb_platform.storage_object_wire.monotonic', return_value=300):
            self.write(refreshed)
        self.assertNotIn(first, self.endpoint._handles)
        self.assertTrue(self.streams[0].closed)
        self.assertIn(refreshed, self.endpoint._handles)
        with patch('kb_platform.storage_object_wire.monotonic', return_value=600):
            with self.assertRaisesRegex(StorageError, 'HANDLE_INVALID'):
                self.call({'op': 'finish_upload', 'handle': refreshed})
        self.assertEqual(self.endpoint._handles, {})
        self.assertTrue(self.streams[1].closed)
        self.assertEqual(self.objects(), [])

    def test_capacity_counts_declared_bytes_and_cancel_is_idempotent(self):
        with patch('kb_platform.storage_object_wire.MAX_HANDLES', 2), \
                patch('kb_platform.storage_object_wire.MAX_TRANSFER_BYTES', 3):
            first = self.begin()
            with self.assertRaisesRegex(StorageError, 'TRANSFER_LIMIT'):
                self.begin()
            empty = self.begin(b'')
            with self.assertRaisesRegex(StorageError, 'TRANSFER_LIMIT'):
                self.begin(b'')
            self.assertEqual(self.call({'op': 'cancel', 'handle': first}), {'closed': True})
            self.assertEqual(self.call({'op': 'cancel', 'handle': first}), {'closed': False})
            self.assertTrue(self.streams[0].closed)
            self.assertIn(empty, self.endpoint._handles)
            self.begin()
        self.assertEqual(self.objects(), [])

    def test_finish_holds_lock_through_actual_put_and_concurrent_close_waits(self):
        entered, release, close_started, close_finished = Event(), Event(), Event(), Event()
        original = self.store.put
        def blocked_put(tenant, data):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('controlled put not released')
            return original(tenant, data)
        def close():
            close_started.set()
            self.endpoint.close()
            close_finished.set()
        handle = self.begin()
        self.write(handle)
        with patch.object(self.store, 'put', side_effect=blocked_put) as put, ThreadPoolExecutor(2) as pool:
            finish = pool.submit(self.call, {'op': 'finish_upload', 'handle': handle})
            try:
                self.assertTrue(entered.wait(5))
                shutdown = pool.submit(close)
                self.assertTrue(close_started.wait(5))
                self.assertFalse(close_finished.is_set())
                acquired = self.endpoint._lock.acquire(blocking=False)
                if acquired:
                    self.endpoint._lock.release()
                self.assertFalse(acquired)
                self.assertFalse(self.streams[0].closed)
            finally:
                release.set()
            result = finish.result(10)
            shutdown.result(10)
            self.assertEqual(put.call_count, 1)
        self.assertTrue(close_finished.is_set())
        self.assertTrue(self.streams[0].closed)
        self.assertEqual(self.endpoint._handles, {})
        self.assertEqual(self.store.read('tenant', decode_value(result['reference']).locator), b'ab')
        with self.assertRaisesRegex(StorageError, 'TRANSFER_CLOSED'):
            self.begin()

    def test_close_failure_seals_admission_but_retry_cleans_remaining_handles(self):
        first, second = self.begin(), self.begin()
        stream = self.streams[0]
        original = stream.close
        def close_then_error():
            original()
            raise OSError('controlled close result failure')
        with patch.object(stream, 'close', side_effect=close_then_error):
            with self.assertRaisesRegex(OSError, 'close result failure'):
                self.endpoint.close()
        self.assertTrue(stream.closed)
        self.assertNotIn(first, self.endpoint._handles)
        self.assertIn(second, self.endpoint._handles)
        self.assertFalse(self.streams[1].closed)
        with self.assertRaisesRegex(StorageError, 'TRANSFER_CLOSED'):
            self.call({'op': 'cancel', 'handle': second})
        self.endpoint.close()
        self.assertTrue(self.streams[1].closed)
        self.assertEqual(self.endpoint._handles, {})


class ObjectTransferSourceBindingContracts(unittest.TestCase):
    def test_all_lifecycle_owners_are_bound_and_semantic_mutations_change_review_digest(self):
        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(item for item in registry['machines'] if item['id'] == 'object_transfer_ownership')
        path = 'kb_platform/storage_object_wire.py'
        source = (root / path).read_text('utf-8')
        symbols = python_symbols(source)
        expected = {name for name in symbols if name.startswith(('ObjectTransferEndpoint.', 'ObjectTransferClient.'))}
        bound = {item['symbol'] for item in machine['source_bindings'] if item['path'] == path}
        self.assertTrue(expected <= bound)
        for binding in machine['source_bindings']:
            self.assertEqual(binding['sha256'], source_digest(root, binding))
        mutants = [('if self._closed:', 'if False:', 'ObjectTransferEndpoint.execute'),
                   ('self._handles.pop(handle, None)', 'self._handles.get(handle)', 'ObjectTransferEndpoint._discard'),
                   ('self._discard(handle)\n\n    def close', 'pass\n\n    def close', 'ObjectTransferEndpoint.execute'),
                   ('self._cancel(handle)', 'pass', 'ObjectTransferClient.put'),
                   ('>= HANDLE_SECONDS', '> HANDLE_SECONDS', 'ObjectTransferEndpoint.execute')]
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            target = temporary / path
            target.parent.mkdir(parents=True)
            for old, new, symbol in mutants:
                with self.subTest(symbol=symbol, mutation=new):
                    self.assertIn(old, source)
                    target.write_text(source.replace(old, new), encoding='utf-8')
                    binding = next(item for item in machine['source_bindings'] if item['symbol'] == symbol)
                    self.assertNotEqual(binding['sha256'], source_digest(temporary, binding))
