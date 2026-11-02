from dataclasses import asdict
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import select

from kb_platform.database_contracts import CommitUnknown, DatabaseError
from kb_platform.application_storage import StorageClient
from kb_platform.plugin_native import native_call
from kb_platform.storage_process import decode_value, encode_value
from kb_platform.storage_protocol import (
    PROTOCOL, resolve_commit, validate_identity, validate_structured, validate_values,
)
from kb_platform.storage_sdk import Document, SpaceKey, StorageError, StorageIdentity

from .storage_json_wire_contracts import payload_store
from .application_storage_contracts import objects
from . import storage_process_contracts


class RustStorageServiceContracts(unittest.TestCase):
    def test_result_validation_after_a_write_preserves_completed_effect_without_replay(self):
        with objects() as service:
            bound = service._binding('originals.objects')
            put, recorded = bound._implementation.put, []

            def invalid_result(*args, **kwargs):
                recorded.append(put(*args, **kwargs))
                return object()

            with patch.object(bound._implementation, 'put', side_effect=invalid_result):
                with self.assertRaisesRegex(StorageError, 'STORAGE_WRITE_RESULT_UNAVAILABLE') as caught:
                    service._invoke(bound, {'version': 1, 'op': 'invoke', 'method': 'put',
                        'args': [encode_value('tenant'), encode_value(b'persisted')], 'kwargs': {}})
            self.assertEqual(caught.exception.details, {'method': 'put', 'outcome': 'COMPLETED'})
            self.assertEqual(len(recorded), 1)
            self.assertEqual(bound.read('tenant', recorded[0].locator), b'persisted')

        values, calls = {}, []

        def invoke(request):
            command = request['request']
            calls.append(command['method'])
            key, value = (decode_value(item) for item in command['args'])
            values[key] = value
            return {'type': 'json', 'value': {}}

        dependency = SimpleNamespace(describe=lambda request: {
            'identity': asdict(StorageIdentity(SpaceKey('app', 'cache', 'primary'), 'dataset')),
            'methods': ['get', 'set', 'delete']}, invoke=invoke)
        client = StorageClient(dependency, 'cache')
        with self.assertRaisesRegex(StorageError, 'STORAGE_WRITE_RESULT_UNAVAILABLE') as caught:
            client.set('key', b'persisted')
        self.assertEqual(caught.exception.details, {'method': 'set', 'outcome': 'COMPLETED'})
        self.assertEqual(values, {'key': b'persisted'})
        self.assertEqual(calls, ['set'])

    def test_native_identity_and_capability_rejection_are_used_before_database_calls(self):
        identity = asdict(StorageIdentity(SpaceKey('app', 'owner', 'space'), 'dataset'))
        validate_identity(identity, expected=identity)
        for part in ('application_id', 'owner_id', 'space_id'):
            changed = {**identity, 'space': {**identity['space'], part: 'another'}}
            with self.subTest(part=part), self.assertRaisesRegex(StorageError, 'STORAGE_DATASET_IDENTITY_MISMATCH'):
                validate_identity(identity, expected=changed)
        with self.assertRaisesRegex(StorageError, 'STORAGE_DATASET_IDENTITY_MISMATCH'):
            validate_identity(identity, expected={**identity, 'dataset_id': 'another'})
        for changed in ({'operation_id': ''}, {'write': 1}, {'limits': {'max_rows': True}},
                        {'limits': {'max_seconds': 301}}, {'dataset_id': 'unbound'}):
            request = {'version': 1, 'op': 'begin', 'operation_id': 'operation',
                       'write': True, 'scope': {}, 'limits': {}, **changed}
            with self.subTest(changed=changed), self.assertRaises(DatabaseError):
                validate_structured(request)

    def test_typed_values_use_bounded_native_batches_and_preserve_the_original_payload(self):
        payload = {'large': 'x' * (5 * 1024 * 1024),
                   'records': [Document(str(index), {'type': 'bytes', 'value': 'literal'}, 1)
                               for index in range(400)], 'binary': b'\x00\xff'}
        encoded = encode_value(payload)
        observed = []

        def track(command, **fields):
            observed.append((command, len(json.dumps(fields).encode())))
            return native_call(command, **fields)

        with patch('kb_platform.storage_protocol.native_call', side_effect=track):
            validate_values([encoded])
        self.assertEqual(decode_value(encoded), payload)
        self.assertGreater(len(observed), 1)
        self.assertTrue(all(command == 'storage_value_validate' for command, _ in observed))
        self.assertLess(max(size for _, size in observed), 512 * 1024 + 4096)
        validate_values([{'type': 'bytes', 'value': base64.b64encode(b'x' * (300 * 1024)).decode('ascii')}], max_nodes=1)
        for invalid in ({'type': 'json', 'value': []}, {'type': 'bytes', 'value': '?'},
                        {'type': 'document', 'value': {'id': 'x', 'revision': True,
                                                      'value': {'type': 'mapping', 'value': {}}}},
                        {'type': {}, 'value': None}):
            with self.subTest(invalid=invalid), self.assertRaises(StorageError):
                validate_values([invalid])

    def test_actual_sqlite_calls_reach_rust_without_replacing_the_sql_driver(self):
        fixture = storage_process_contracts.StorageProcessContracts(
            'test_real_sqlite_kv_process_calls_are_binary_safe_and_bound_to_contract')
        fixture.setUp()
        observed = []

        def track(command, **fields):
            observed.append(command)
            return native_call(command, **fields)

        try:
            with patch('kb_platform.storage_protocol.native_call', side_effect=track):
                fixture.test_real_sqlite_kv_process_calls_are_binary_safe_and_bound_to_contract()
            self.assertIn('storage_invocation_validate', observed)
            self.assertIn('storage_value_validate', observed)
        finally:
            fixture.doCleanups()

    def test_large_sqlite_transaction_and_receipt_use_rust_and_keep_native_frames_small(self):
        observed = []

        def track(command, **fields):
            observed.append((command, len(json.dumps(fields).encode())))
            return native_call(command, **fields)

        with patch('kb_platform.storage_protocol.native_call', side_effect=track):
            with payload_store() as (client, _, table, messages):
                body = {'text': 'data survives the native boundary ' * 160000}
                with client.transaction(operation_id='large-physical-write') as transaction:
                    transaction.insert(table, {'id': 1, 'payload': body})
                with client.transaction(write=False) as transaction:
                    self.assertEqual(transaction.execute(select(table.c.payload)).scalar(), body)
                self.assertIn('wire_dispatch', [json.loads(message)['op'] for message in messages])
        self.assertIn('storage_structured_validate', [command for command, _ in observed])
        self.assertIn('storage_commit_resolve', [command for command, _ in observed])
        self.assertLess(max(size for _, size in observed), 4096)

    def test_commit_unknown_preserves_the_original_operation_and_never_becomes_a_retry(self):
        for response in (None, {}, {'status': 'COMMITTED', 'operation_id': 'other'},
                         {'status': 'UNKNOWN', 'operation_id': 'physical'}):
            with self.subTest(response=response), self.assertRaises(CommitUnknown) as caught:
                resolve_commit('physical', response)
            self.assertEqual(caught.exception.operation_id, 'physical')
        receipt = resolve_commit('physical', {'status': 'COMMITTED', 'operation_id': 'physical'})
        self.assertFalse(receipt['replay_allowed'])
        with self.assertRaisesRegex(DatabaseError, 'STORAGE_TRANSACTION_ROLLED_BACK'):
            resolve_commit('physical', {'status': 'ROLLED_BACK', 'operation_id': 'physical'})
        result = native_call('storage_commit_resolve', protocol=PROTOCOL,
                             operation_id='physical', response={'status': 'UNKNOWN', 'operation_id': 'physical'})
        self.assertEqual(result, {'operation_id': 'physical', 'status': 'UNKNOWN', 'replay_allowed': False})
