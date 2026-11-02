import base64
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import Column, Integer, MetaData, Numeric, Table, func, select

from kb_platform.application_storage import ApplicationStorage
from kb_platform.database_contracts import CommitUnknown, DatabaseError
from kb_platform.documents import parse_native_document
from kb_platform.knowledge_authority_contract import METHODS, AuthorityStorageEndpoint, authority_database
from kb_platform.knowledge_queries import KnowledgeQuery
from kb_platform.knowledge_schema import PARSE_SNAPSHOTS
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.plugin_contracts import ServiceEndpoint
from kb_platform.plugin_native import PluginError
from kb_platform.session_identity import Actor
from kb_platform.storage_catalog import initialize_configuration
from kb_platform.storage_expression_wire import datatype, restored_type, schema_descriptor, schema_semantics, statement
from kb_platform.storage_json_wire import (
    CHUNK_BYTES, MAX_MESSAGE_BYTES, MAX_VALUE_BYTES, StorageJsonEndpoint,
)
from kb_platform.storage_sdk.relational import StructuredSchema, TypedJSON

from .application_storage_contracts import JsonDependency
from .storage_structured_wire_contracts import structured


@contextmanager
def payload_store():
    metadata = MetaData()
    table = Table('payloads', metadata, Column('id', Integer, primary_key=True), Column('payload', TypedJSON))
    schema = StructuredSchema('payloads', metadata)
    with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, messages):
        client.prepare(schema)
        client.seal()
        yield client, endpoint, table, messages


class StorageJsonWireContracts(unittest.TestCase):
    def test_parsed_document_above_single_frame_roundtrips_through_default_authority_contract(self):
        data = (b'Evidence line with number 42.\n' * 4000)[:100000]
        document = parse_native_document(data, 'evidence.txt')
        self.assertEqual(document['status'], 'READY')
        self.assertGreater(len(json.dumps(document).encode()), 4 * 1024 * 1024)
        with TemporaryDirectory() as directory:
            configuration = initialize_configuration({'version': 2, 'application_id': 'large-authority',
                'instances': {'records': {'provider': 'sqlite', 'config': {'root': str(Path(directory) / 'records')}}},
                'services': {'records': {'instance': 'records', 'export': 'structured'}},
                'bindings': {'repository': {'primary': {
                    'service': 'records', 'owner': 'knowledge', 'space': 'authority'}}}})
            storage = ApplicationStorage(configuration)
            try:
                dependency = JsonDependency(ServiceEndpoint(AuthorityStorageEndpoint(storage), METHODS))
                database = authority_database(dependency)
                repository = KnowledgeRepository(database=database)
                actor = Actor('tenant', 'owner')
                digest = sha256(data).hexdigest()
                with repository.transaction(actor) as transaction:
                    transaction.execute(KnowledgeQuery.SOURCE_INSERT, ('tenant', 'source', 'owner', [], {}))
                    transaction.execute(KnowledgeQuery.SOURCE_VERSION_INSERT,
                        ('tenant', 'source', digest, 'evidence.txt', 'original', digest, document['media_type']))
                    transaction.execute(KnowledgeQuery.PARSE_INSERT,
                        ('tenant', 'source', digest, document['parse_snapshot_id'], document))
                with database.transaction(write=False, scope={'tenant_id': 'tenant'}) as transaction:
                    self.assertEqual(transaction.execute(select(PARSE_SNAPSHOTS.c.document)).scalar(), document)
                with database.transaction(write=False, scope={'tenant_id': 'other'}) as transaction:
                    self.assertEqual(transaction.execute(select(PARSE_SNAPSHOTS.c.document)).rows, ())
                messages = [json.loads(value) for _, value in dependency.messages]
                self.assertIn('wire_dispatch', [message.get('op') for message in messages])
                self.assertIn('wire_read', [message.get('op') for message in messages])
                self.assertLess(max(len(value.encode()) for _, value in dependency.messages), 4 * 1024 * 1024)
                endpoint = storage._structured['repository.primary']
                self.assertEqual(endpoint._transport._transfers, {})
                self.assertEqual(endpoint._handles, {})
            finally:
                storage.close()

    def test_interrupted_upload_rolls_back_and_closes_all_transfer_files(self):
        with payload_store() as (client, endpoint, table, _):
            original, streams, writes = client._send, [], []

            def interrupt(request):
                if request['op'] == 'wire_write':
                    writes.append(request['offset'])
                    streams.extend(item.stream for item in endpoint._transport._transfers.values())
                    raise PluginError('PROCESS_RESULT_UNKNOWN')
                return original(request)

            with patch.object(client, '_send', side_effect=interrupt), self.assertRaises(PluginError):
                with client.transaction() as transaction:
                    transaction.insert(table, {'id': 1, 'payload': {'text': 'x' * MAX_MESSAGE_BYTES}})
            self.assertEqual(writes, [0])
            self.assertTrue(streams and all(stream.closed for stream in streams))
            self.assertEqual(endpoint._transport._transfers, {})
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(func.count()).select_from(table)).scalar(), 0)

    def test_dispatch_acknowledgement_loss_does_not_repeat_insert_and_rolls_back(self):
        with payload_store() as (client, endpoint, table, _):
            original, dispatches = client._send, []

            def lose_acknowledgement(request):
                result = original(request)
                if request['op'] == 'wire_dispatch':
                    dispatches.append(request['handle'])
                    raise PluginError('PROCESS_RESULT_UNKNOWN')
                return result

            with patch.object(client, '_send', side_effect=lose_acknowledgement), self.assertRaises(PluginError):
                with client.transaction() as transaction:
                    transaction.insert(table, {'id': 1, 'payload': {'text': 'x' * MAX_MESSAGE_BYTES}})
            self.assertEqual(len(dispatches), 1)
            self.assertEqual(endpoint._transport._transfers, {})
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(table)).rows, ())

    def test_lost_response_descriptor_is_released_by_transaction_rollback(self):
        with payload_store() as (client, endpoint, table, _):
            body = {'text': 'x' * MAX_MESSAGE_BYTES}
            with client.transaction() as transaction:
                transaction.insert(table, {'id': 1, 'payload': body})
            original, streams = client._send, []

            def lose_descriptor(request):
                result = original(request)
                if isinstance(result, dict) and 'wire_result' in result:
                    streams.extend(item.stream for item in endpoint._transport._transfers.values())
                    raise PluginError('PROCESS_RESULT_UNKNOWN')
                return result

            with patch.object(client, '_send', side_effect=lose_descriptor), self.assertRaises(PluginError):
                with client.transaction(write=False) as transaction:
                    transaction.execute(select(table.c.payload))
            self.assertTrue(streams and all(stream.closed for stream in streams))
            self.assertEqual(endpoint._transport._transfers, {})

    def test_response_capacity_failure_rolls_back_mutation_inside_transaction_worker(self):
        with payload_store() as (client, endpoint, table, _):
            body = {'text': 'x' * MAX_MESSAGE_BYTES}
            with patch('kb_platform.storage_json_wire.MAX_TRANSFERS', 1):
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_LIMIT'):
                    with client.transaction() as transaction:
                        transaction.insert(table, {'id': 1, 'payload': body}, returning=(table.c.payload,))
            self.assertEqual(endpoint._transport._transfers, {})
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(table)).rows, ())

    def test_download_interruption_and_corruption_cancel_remaining_response(self):
        with payload_store() as (client, endpoint, table, _):
            with client.transaction() as transaction:
                transaction.insert(table, {'id': 1, 'payload': {'text': 'x' * MAX_MESSAGE_BYTES}})
            original = client._send
            for failure in ('interrupted', 'corrupt'):
                with self.subTest(failure=failure):
                    streams, reads = [], []

                    def broken_read(request):
                        if request['op'] != 'wire_read':
                            return original(request)
                        streams.extend(item.stream for item in endpoint._transport._transfers.values())
                        reads.append(request['offset'])
                        if failure == 'interrupted':
                            raise PluginError('PROCESS_RESULT_UNKNOWN')
                        result = original(request)
                        if request['offset'] == 0:
                            data = base64.b64decode(result['data_base64'])
                            result['data_base64'] = base64.b64encode(b'x' + data[1:]).decode()
                        return result

                    with patch.object(client, '_send', side_effect=broken_read):
                        with self.assertRaises((PluginError, DatabaseError)):
                            with client.transaction(write=False) as transaction:
                                transaction.execute(select(table.c.payload))
                    self.assertTrue(reads and streams and all(stream.closed for stream in streams))
                    self.assertEqual(endpoint._transport._transfers, {})

    def test_logical_size_limit_and_single_message_limit_still_reject_before_dispatch(self):
        with payload_store() as (client, endpoint, table, _):
            request = {'version': 1, 'op': 'execute', 'handle': 'invalid', 'payload': 'x' * MAX_MESSAGE_BYTES}
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_MESSAGE_LIMIT'):
                endpoint.execute(request)
            send = Mock(side_effect=AssertionError('oversized logical request was sent'))
            with patch('kb_platform.storage_json_wire.MAX_VALUE_BYTES', 1024), patch.object(client, '_send', send):
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_MESSAGE_LIMIT'):
                    client._call(request)
            send.assert_not_called()
            self.assertEqual(endpoint._transport._transfers, {})

    def test_large_write_commit_acknowledgement_loss_remains_unknown_and_is_not_replayed(self):
        with payload_store() as (client, endpoint, table, _):
            original, finishes = client._send, []

            def lose_commit(request):
                result = original(request)
                if request['op'] == 'finish' and request['commit']:
                    finishes.append(request['handle'])
                    raise PluginError('PROCESS_RESULT_UNKNOWN')
                return result

            with patch.object(client, '_send', side_effect=lose_commit):
                with self.assertRaises(CommitUnknown) as failure:
                    with client.transaction(operation_id='large-write') as transaction:
                        transaction.insert(table, {'id': 1, 'payload': {'text': 'x' * MAX_MESSAGE_BYTES}})
            self.assertEqual(failure.exception.operation_id, 'large-write')
            self.assertEqual(len(finishes), 1)
            self.assertEqual(endpoint._transport._transfers, {})
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(func.count()).select_from(table)).scalar(), 1)

    def test_transfer_capacity_offsets_hash_expiration_and_close_are_bounded(self):
        handler = Mock()
        endpoint = StorageJsonEndpoint(handler, lambda token: None)
        self.addCleanup(endpoint.close)
        data = json.dumps({'version': 1, 'op': 'execute', 'handle': 'transaction'}).encode()
        begin = {'version': 1, 'op': 'wire_begin', 'bytes': len(data),
                 'sha256': sha256(data).hexdigest(), 'transaction': 'transaction'}
        with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_INVALID'):
            endpoint.execute({**begin, 'bytes': MAX_VALUE_BYTES + 1})
        with patch('kb_platform.storage_json_wire.MAX_TRANSFER_BYTES', len(data)), \
                patch('kb_platform.storage_json_wire.monotonic', return_value=0):
            opened = endpoint.execute(begin)
            handle = opened['handle']
            stream = endpoint._transfers[handle].stream
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_LIMIT'):
                endpoint.execute(begin)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_OFFSET_INVALID'):
                endpoint.execute({'version': 1, 'op': 'wire_write', 'handle': handle,
                    'offset': 1, 'data_base64': base64.b64encode(data).decode()})
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_LIMIT'):
                endpoint.execute({'version': 1, 'op': 'wire_write', 'handle': handle,
                    'offset': 0, 'data_base64': base64.b64encode(b'x' * (CHUNK_BYTES + 1)).decode()})
        with patch('kb_platform.storage_json_wire.monotonic', return_value=301):
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_CLOSED'):
                endpoint.execute({'version': 1, 'op': 'wire_dispatch', 'handle': handle})
        self.assertTrue(stream.closed)
        for payload in (b'', b'x' * len(data)):
            opened = endpoint.execute(begin)
            if payload:
                endpoint.execute({'version': 1, 'op': 'wire_write', 'handle': opened['handle'],
                    'offset': 0, 'data_base64': base64.b64encode(payload).decode()})
            with self.assertRaises(DatabaseError):
                endpoint.execute({'version': 1, 'op': 'wire_dispatch', 'handle': opened['handle']})
        handler.assert_not_called()
        opened = endpoint.execute(begin)
        stream = endpoint._transfers[opened['handle']].stream
        endpoint.close()
        self.assertTrue(stream.closed)
        self.assertEqual(endpoint._transfers, {})
        with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_CLOSED'):
            endpoint.execute(begin)

    def test_structured_count_star_and_nullable_count_column_keep_different_semantics(self):
        with payload_store() as (client, _, table, _):
            with client.transaction() as transaction:
                transaction.insert(table, {'id': 1, 'payload': None})
                transaction.insert(table, {'id': 2, 'payload': {'present': True}})
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(func.count()).select_from(table)).scalar(), 2)
                self.assertEqual(transaction.execute(select(func.count(table.c.payload))).scalar(), 1)
            self.assertEqual(statement(select(func.count()).select_from(table))['columns'][0]['args'], [])

    def test_decimal_schema_negotiation_and_literal_roundtrip_preserve_precision_and_scale(self):
        schemas = []
        for precision, scale in ((12, 2), (6, 4)):
            metadata = MetaData()
            Table('amounts', metadata, Column('id', Integer, primary_key=True),
                  Column('amount', Numeric(precision, scale), nullable=False))
            schemas.append(StructuredSchema('amounts', metadata))
        self.assertNotEqual(*(schema_semantics(schema_descriptor(schema, include_digest=False)) for schema in schemas))
        expected = datatype(Numeric(12, 2, asdecimal=False, decimal_return_scale=3))
        self.assertEqual(datatype(restored_type(expected)), expected)
        with TemporaryDirectory() as directory, structured(directory, schemas[0]) as (client, _, _):
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_SCHEMA_UNDECLARED'):
                client.prepare(schemas[1])
            client.prepare(schemas[0])
            client.seal()
            table = schemas[0].metadata.tables['amounts']
            with client.transaction() as transaction:
                transaction.insert(table, {'id': 1, 'amount': Decimal('12.34')})
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(table.c.amount)
                    .where(table.c.amount == Decimal('12.34'))).scalar(), Decimal('12.34'))
        for changed in ({'precision': True}, {'precision': 0}, {'precision': 2, 'scale': 3},
                        {'scale': -1}, {'decimal_return_scale': -1}, {'as_decimal': 1}):
            with self.subTest(changed=changed), self.assertRaises(DatabaseError):
                restored_type({**expected, **changed})
