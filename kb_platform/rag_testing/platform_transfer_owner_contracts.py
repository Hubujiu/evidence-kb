import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.knowledge_platform_contract import METHODS, PlatformEndpoint
from kb_platform.platform_transfers import PlatformSnapshots
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.session_identity import PlatformError


ACTOR = {'tenant_id': 'tenant', 'principal_id': 'owner'}


class PlatformTransferOwnerContracts(unittest.TestCase):
    def fixture(self, ingest):
        endpoint = PlatformEndpoint(SimpleNamespace(ingest=ingest))
        self.addCleanup(endpoint.close)
        return endpoint, ServiceEndpoint(endpoint, METHODS)

    def upload(self, service, data=b'ab', digest=None):
        request = service.ingest_begin({'actor': ACTOR, 'kb_id': 'kb', 'filename': 'source.txt',
            'readers': None, 'policy': None, 'source_id': None, 'size': len(data),
            'sha256': digest or sha256(data).hexdigest()})
        token = {**request, 'actor': ACTOR}
        for offset, chunk in enumerate(data):
            service.ingest_append({**token, 'offset': offset,
                                  'data_base64': base64.b64encode(bytes([chunk])).decode()})
        return token

    def test_capacity_rejection_preserves_complete_upload_for_explicit_finish(self):
        calls = []
        endpoint, service = self.fixture(lambda *args, **kwargs: calls.append(kwargs['data']) or {'source_id': 's'})
        token = self.upload(service)
        reserved = endpoint._transfers.reserve(ACTOR, 'original')
        with patch('kb_platform.platform_transfers.MAX_TRANSFERS', 1):
            with self.assertRaises(ContractError) as caught:
                service.ingest_finish(token)
            self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFERS_BUSY')
            self.assertIn(token['upload_id'], endpoint._uploads)
            self.assertEqual(calls, [])
            endpoint._transfers.cancel(ACTOR, reserved)
            self.assertEqual(service.ingest_finish(token), {'result': {'source_id': 's'}})
        self.assertEqual(calls, [b'ab'])
        self.assertEqual(endpoint._uploads, {})

    def test_real_file_consumed_before_unlocked_ingest_and_duplicate_finish_refused(self):
        entered, release = Event(), Event()
        calls = []

        def ingest(*args, **kwargs):
            calls.append(kwargs['data'])
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture release missing')
            return {'source_id': 's'}

        endpoint, service = self.fixture(ingest)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.txt'
            path.write_bytes(b'ab')
            token = self.upload(service, path.read_bytes())
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.ingest_finish, token)
            try:
                self.assertTrue(entered.wait(3))
                self.assertNotIn(token['upload_id'], endpoint._uploads)
                self.assertEqual(len(endpoint._transfers._handles), 1)
                self.assertEqual(pool.submit(service.ingest_cancel, token).result(3), {'cancelled': False})
                with self.assertRaises(ContractError):
                    pool.submit(service.ingest_finish, token).result(3)
            finally:
                release.set()
            self.assertEqual(first.result(3), {'result': {'source_id': 's'}})
        self.assertEqual(calls, [b'ab'])

    def test_committed_ingest_publication_failure_never_restores_upload_or_repeats_effect(self):
        calls = []
        endpoint, service = self.fixture(lambda *args, **kwargs: calls.append(kwargs['data']) or {'bad': float('nan')})
        token = self.upload(service)
        with self.assertRaises(ContractError):
            service.ingest_finish(token)
        with self.assertRaises(ContractError):
            service.ingest_finish(token)
        self.assertEqual(calls, [b'ab'])
        self.assertEqual((endpoint._uploads, endpoint._transfers._handles), ({}, {}))

    def test_upload_deadline_is_strict_and_actor_cannot_cancel_another_upload(self):
        endpoint, service = self.fixture(lambda *args, **kwargs: {'source_id': 's'})
        with patch('kb_platform.knowledge_platform_contract.monotonic', return_value=0):
            token = self.upload(service)
        foreign = {**token, 'actor': {**ACTOR, 'principal_id': 'other'}}
        with patch('kb_platform.knowledge_platform_contract.monotonic', return_value=900):
            self.assertEqual(service.ingest_cancel(foreign), {'cancelled': False})
            self.assertIn(token['upload_id'], endpoint._uploads)
        with patch('kb_platform.knowledge_platform_contract.monotonic', return_value=900.1):
            with self.assertRaises(ContractError):
                service.ingest_finish(token)
        self.assertEqual(endpoint._uploads, {})

    def snapshot(self):
        snapshots = PlatformSnapshots()
        self.addCleanup(snapshots.close)
        token = snapshots.reserve(ACTOR, 'original')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'original.bin'
            path.write_bytes(b'ab')
            snapshots.publish(token, [path.read_bytes()], {'source': 'immutable'})
        return snapshots, token, snapshots._handles[token].stream

    def test_authorization_runs_outside_lock_and_cancel_wins_before_stream_read(self):
        snapshots, token, stream = self.snapshot()

        finished, outcomes, workers = Event(), [], []

        def authorize(context):
            def cancel():
                outcomes.append(snapshots.cancel(ACTOR, token))
                finished.set()
            worker = Thread(target=cancel, daemon=True)
            workers.append(worker)
            worker.start()
            self.assertTrue(finished.wait(3))

        try:
            with self.assertRaises(PlatformError) as caught:
                snapshots.read(ACTOR, token, 0, 'original', authorize)
        finally:
            for worker in workers:
                worker.join(3)
                self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, [True])
        self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFER_UNAVAILABLE')
        self.assertTrue(stream.closed)
        self.assertEqual(snapshots._handles, {})

    def test_close_or_exact_expiry_during_authorization_rechecked_before_read(self):
        for interference in ('close', 'expiry'):
            with self.subTest(interference=interference):
                snapshots, token, stream = self.snapshot()
                clock = [0]

                def authorize(context):
                    if interference == 'close':
                        snapshots.close()
                    else:
                        clock[0] = snapshots._handles[token].expires

                with patch('kb_platform.platform_transfers.monotonic', side_effect=lambda: clock[0]):
                    with self.assertRaises(PlatformError):
                        snapshots.read(ACTOR, token, 0, 'original', authorize)
                self.assertTrue(stream.closed)
                self.assertEqual(snapshots._handles, {})

    def test_two_readers_same_offset_have_only_one_chunk_winner(self):
        snapshots, token, stream = self.snapshot()
        entered, release = Event(), Event()

        def slow_authorize(context):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture release missing')

        with patch('kb_platform.platform_transfers.CHUNK_BYTES', 1), ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(snapshots.read, ACTOR, token, 0, 'original', slow_authorize)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(snapshots.read(ACTOR, token, 0, 'original', lambda context: None)['data'], b'a')
            finally:
                release.set()
            with self.assertRaises(PlatformError) as caught:
                slow.result(3)
            self.assertEqual(caught.exception.code, 'INVALID_PLATFORM_TRANSFER')
            self.assertEqual(snapshots.read(ACTOR, token, 1, 'original', lambda context: None)['data'], b'b')
        self.assertTrue(stream.closed)

    def test_reserved_capacity_does_not_expire_and_cancelled_publication_closes_temporary_stream(self):
        snapshots = PlatformSnapshots()
        self.addCleanup(snapshots.close)
        with patch('kb_platform.platform_transfers.monotonic', return_value=0):
            token = snapshots.reserve(ACTOR, 'ingest')
        with patch('kb_platform.platform_transfers.monotonic', return_value=10000), \
                patch('kb_platform.platform_transfers.MAX_TOTAL_BYTES', 64 * 1024 * 1024):
            with self.assertRaises(PlatformError) as caught:
                snapshots.reserve(ACTOR, 'original')
            self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFERS_BUSY')
        snapshots.cancel(ACTOR, token)
        from tempfile import SpooledTemporaryFile
        stream = SpooledTemporaryFile(mode='w+b')
        with patch('kb_platform.platform_transfers.SpooledTemporaryFile', return_value=stream):
            with self.assertRaises(PlatformError):
                snapshots.publish(token, [b'ab'], {})
        self.assertTrue(stream.closed)
        self.assertEqual(snapshots._handles, {})


if __name__ == '__main__':
    unittest.main()
