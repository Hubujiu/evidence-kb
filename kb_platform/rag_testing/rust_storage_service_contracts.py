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
from kb_platform.storage_sdk import Document, KV_DURABLE_V1, SpaceKey, StorageError, StorageIdentity

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
            'contract': {'id': 'storage.kv', 'major': 1, 'extends': []},
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
        observed, chunks = [], []

        def track(command, **fields):
            observed.append((command, len(json.dumps(fields, ensure_ascii=False).encode())))
            chunks.extend(event['value'] for event in fields.get('events', []) if event['event'] == 'chunk')
            return native_call(command, **fields)

        with patch('kb_platform.storage_protocol.native_call', side_effect=track):
            validate_values([encoded])
        self.assertEqual(decode_value(encoded), payload)
        self.assertGreater(len(observed), 1)
        self.assertTrue(all(command == 'storage_service' for command, _ in observed))
        self.assertEqual(sum(len(value) for value in chunks if value and set(value) == {'x'}), len(payload['large']))
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
            observed.append((command, fields.get('action')))
            return native_call(command, **fields)

        try:
            with patch('kb_platform.storage_protocol.native_call', side_effect=track):
                fixture.test_real_sqlite_kv_process_calls_are_binary_safe_and_bound_to_contract()
            for action in ('open', 'feed', 'admit', 'result', 'finish'):
                self.assertIn(('storage_service', action), observed)
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

    def test_local_and_wire_calls_use_one_rust_admission_before_any_mutation(self):
        fixture = storage_process_contracts.StorageProcessContracts()
        fixture.setUp()
        try:
            bound = fixture.bound('kv', KV_DURABLE_V1)
            with patch.object(bound._implementation, 'set', wraps=bound._implementation.set) as writes:
                with self.assertRaisesRegex(StorageError, 'STORAGE_INVALID_PROCESS_REQUEST'):
                    bound.set('record', 'text is not bytes')
                with self.assertRaisesRegex(StorageError, 'STORAGE_INVALID_PROCESS_REQUEST'):
                    bound.call_json({'method': 'set', 'args': [encode_value('record'), encode_value(True)]})
                with self.assertRaisesRegex(StorageError, 'STORAGE_INVALID_PROCESS_REQUEST'):
                    bound.call_json({'method': 'set', 'args': [encode_value('record'), encode_value(b'content')],
                                     'kwargs': {'key': encode_value('duplicate')}})
                writes.assert_not_called()
                bound.set('record', b'content')
                self.assertEqual(writes.call_count, 1)
            self.assertEqual(bound.get('record'), b'content')
        finally:
            fixture.doCleanups()

    def test_failed_stream_cannot_resume_and_scoped_validators_release_capacity(self):
        def native(action, **fields):
            return native_call('storage_service', protocol=PROTOCOL, action=action, **fields)

        handle = native('values', limits={'max_nodes': 10, 'max_bytes': 4096})['result']['handle']
        try:
            rejected = native('feed', handle=handle, events=[{'event': 'open', 'type': 'mapping', 'length': 1},
                {'event': 'scalar', 'type': 'integer', 'value': '7'}])
            self.assertFalse(rejected['ok'])
            retry = native('feed', handle=handle, events=[{'event': 'key', 'value': 'missing'},
                {'event': 'scalar', 'type': 'integer', 'value': '7'}, {'event': 'close'}])
            self.assertFalse(retry['ok'])
            self.assertEqual(retry['error']['code'], 'STORAGE_CALL_STATE_INVALID')
        finally:
            native('close', handle=handle)
        for attempt in range(70):
            with self.subTest(attempt=attempt), self.assertRaises(StorageError):
                validate_values([{'type': 'document', 'value': {'id': 'record', 'revision': 0,
                                  'value': {'type': 'mapping', 'value': {}}}}])
        validate_values([encode_value({'released': True})])

    def test_completed_write_is_preserved_when_result_publication_fails(self):
        from kb_platform.storage_json_wire import StorageJsonEndpoint

        with objects() as service:
            bound = service._binding('originals.objects')
            original, written = bound._implementation.put, []

            def put(*args, **kwargs):
                reference = original(*args, **kwargs)
                written.append(reference)
                return reference

            with patch.object(bound._implementation, 'put', side_effect=put):
                with patch.object(StorageJsonEndpoint, 'result', side_effect=DatabaseError('STORAGE_WIRE_MESSAGE_LIMIT')):
                    with self.assertRaisesRegex(StorageError, 'STORAGE_WRITE_RESULT_UNAVAILABLE') as caught:
                        service.invoke({'binding': 'originals.objects', 'request': {'version': 1, 'op': 'invoke',
                            'method': 'put', 'args': [encode_value('tenant'), encode_value(b'persisted')], 'kwargs': {}}})
            self.assertEqual(caught.exception.details, {'method': 'put', 'outcome': 'COMPLETED'})
            self.assertEqual(len(written), 1)
            self.assertEqual(bound.read('tenant', written[0].locator), b'persisted')

    def test_any_missing_commit_receipt_preserves_unknown_after_the_real_write(self):
        with payload_store() as (client, _, table, _):
            original, commits = client._send, []

            def lost_receipt(request):
                result = original(request)
                if request['op'] == 'finish' and request['commit']:
                    commits.append(request)
                    raise OSError('synthetic delivery interruption')
                return result

            with patch.object(client, '_send', side_effect=lost_receipt):
                with self.assertRaises(CommitUnknown) as caught:
                    with client.transaction(operation_id='physical-unclassified-delivery') as transaction:
                        transaction.insert(table, {'id': 1, 'payload': {'committed': True}})
            self.assertEqual(caught.exception.operation_id, 'physical-unclassified-delivery')
            self.assertEqual(len(commits), 1)
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(table.c.payload)).scalar(), {'committed': True})

    def test_bound_expiring_kv_preserves_ttl_errors_before_local_or_wire_writes(self):
        from kb_platform.storage_sdk import EphemeralCacheSpace, KV_EXPIRING_V1
        from kb_platform.storage_service import StorageService
        from . import storage_values_contracts

        fixture = storage_values_contracts.RedisCacheBoundaryContracts()
        fixture.setUp()
        registry = StorageService()
        try:
            registry.register('cache', fixture.provider.exports()[0])
            bound = registry.bind(registry.grant('cache', KV_EXPIRING_V1, fixture.space, 'consumer',
                target=EphemeralCacheSpace('cache-identity')))
            with patch.object(bound._implementation, 'put_expiring', wraps=bound._implementation.put_expiring) as adapter:
                for ttl in (0, -1, True, None, 'invalid', 366 * 24 * 60 * 60):
                    for mode in ('local', 'wire'):
                        with self.subTest(ttl=ttl, mode=mode), self.assertRaisesRegex(StorageError, 'STORAGE_INVALID_TTL'):
                            if mode == 'local':
                                bound.put_expiring('probe', b'must-not-write', ttl_seconds=ttl)
                            else:
                                bound.call_json({'method': 'put_expiring',
                                    'args': [encode_value('probe'), encode_value(b'must-not-write')],
                                    'kwargs': {'ttl_seconds': encode_value(ttl)}})
                adapter.assert_not_called()
                fixture.client.set.assert_not_called()
                bound.put_expiring('local', b'accepted', ttl_seconds=0.0011)
                bound.call_json({'method': 'put_expiring', 'args': [encode_value('wire'), encode_value(b'accepted')],
                                 'kwargs': {'ttl_seconds': encode_value(0.0011)}})
                self.assertEqual(adapter.call_count, 2)
                self.assertEqual(fixture.client.set.call_count, 2)
                self.assertEqual([call.kwargs for call in fixture.client.set.call_args_list], [{'px': 2}, {'px': 2}])
        finally:
            registry.close()
            fixture.doCleanups()
