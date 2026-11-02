"""Implementation binding for storage value-call and transfer ownership models."""
import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from threading import Event
import unittest
from unittest.mock import patch

from kb_platform.contract_transfer import JsonClient, JsonEndpoint, TransferError
from kb_platform.storage_process import encode_value
from kb_platform.storage_sdk import KV_DURABLE_V1, StorageError
from kb_platform import storage_protocol

from .application_storage_contracts import objects
from . import storage_process_contracts


class StorageProtocolOwnershipContracts(unittest.TestCase):
    def test_lost_native_validator_after_real_write_preserves_completion_without_replay(self):
        with objects() as service:
            bound = service._binding('originals.objects')
            original_put, original_service = bound._implementation.put, storage_protocol._service
            writes, closed = [], []

            def put(*args, **kwargs):
                result = original_put(*args, **kwargs)
                writes.append(result)
                return result

            def dispatch(action, **fields):
                if action == 'result':
                    closed.append(fields['handle'])
                    original_service('close', handle=fields['handle'])
                return original_service(action, **fields)

            with patch.object(bound._implementation, 'put', side_effect=put), \
                    patch.object(storage_protocol, '_service', side_effect=dispatch):
                with self.assertRaisesRegex(StorageError, 'STORAGE_WRITE_RESULT_UNAVAILABLE') as caught:
                    service._invoke(bound, {'version': 1, 'op': 'invoke', 'method': 'put',
                        'args': [encode_value('tenant'), encode_value(b'persisted-before-validator-loss')], 'kwargs': {}})
            self.assertEqual(caught.exception.details, {'method': 'put', 'outcome': 'COMPLETED'})
            self.assertEqual(len(closed), 1)
            self.assertEqual(len(writes), 1)
            self.assertEqual(bound.read('tenant', writes[0].locator), b'persisted-before-validator-loss')

    def test_completed_write_capable_method_can_be_a_real_sqlite_no_op(self):
        fixture = storage_process_contracts.StorageProcessContracts()
        fixture.setUp()
        try:
            bound = fixture.bound('kv', KV_DURABLE_V1)
            original, deletions = bound._implementation.delete, []

            def unavailable_result(key):
                deletions.append(original(key))
                return object()

            with patch.object(bound._implementation, 'delete', side_effect=unavailable_result):
                with self.assertRaisesRegex(StorageError, 'STORAGE_WRITE_RESULT_UNAVAILABLE') as caught:
                    bound.delete('missing-key')
            self.assertEqual(caught.exception.details, {'method': 'delete', 'outcome': 'COMPLETED'})
            self.assertEqual(deletions, [False])
            self.assertIsNone(bound.get('missing-key'))
        finally:
            fixture.doCleanups()

    def test_close_and_cancel_preserve_inflight_dispatch_stream_until_handler_finishes(self):
        entered, release, calls = Event(), Event(), []

        def handler(request):
            calls.append(request)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('synthetic handler wait timed out')
            return {'done': True}

        endpoint = JsonEndpoint(handler)
        data = json.dumps({'version': 1, 'op': 'execute', 'value': 'synthetic'}).encode()
        opened = endpoint.execute({'version': 1, 'op': 'wire_begin', 'bytes': len(data),
                                   'sha256': sha256(data).hexdigest(), 'transaction': None})
        token = opened['handle']
        endpoint.execute({'version': 1, 'op': 'wire_write', 'handle': token, 'offset': 0,
                          'data_base64': base64.b64encode(data).decode()})
        stream = endpoint._transfers[token].stream
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(endpoint.execute, {'version': 1, 'op': 'wire_dispatch', 'handle': token})
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(TransferError, 'CONTRACT_WIRE_TRANSFER_BUSY'):
                    endpoint.execute({'version': 1, 'op': 'wire_cancel', 'handle': token})
                endpoint.close()
                self.assertIn(token, endpoint._transfers)
                self.assertFalse(stream.closed)
            finally:
                release.set()
            self.assertEqual(result.result(timeout=5), {'done': True})
        self.assertTrue(stream.closed)
        self.assertEqual(endpoint._transfers, {})
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(TransferError, 'CONTRACT_WIRE_CLOSED'):
            endpoint.execute({'version': 1, 'op': 'wire_dispatch', 'handle': token})

    def test_submission_marker_is_not_proof_of_handler_execution(self):
        sends = []

        def disconnected(request):
            sends.append(request)
            raise OSError('synthetic failure before reaching endpoint')

        client = JsonClient(disconnected)
        with self.assertRaises(OSError) as caught:
            client.call({'version': 1, 'op': 'execute', 'value': 'synthetic'})
        self.assertTrue(caught.exception.request_dispatched)
        self.assertEqual(len(sends), 1)
        # No endpoint/handler was invoked: this marker denotes a submission attempt,
        # not known backend execution, completion, database commit, or model usage.
