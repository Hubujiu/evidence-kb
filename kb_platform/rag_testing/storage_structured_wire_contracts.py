from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select

from kb_platform.application_storage import ApplicationStorage, StorageClient, storage_methods
from kb_platform.knowledge_storage_configuration import SCHEMAS as KNOWLEDGE_SCHEMAS
from kb_platform.database_contracts import CommitUnknown, DatabaseError, QueryLimits, SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.knowledge_schema import knowledge_schema
from kb_platform.persistence_schema import budget_calls, budget_schema
from kb_platform.plugin_contracts import ServiceEndpoint
from kb_platform.plugin_native import PluginError
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_storage import session_schema
from kb_platform.session_store import SessionStore
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_catalog import initialize_configuration
from kb_platform.storage_sdk import ExistingDataset
from kb_platform.storage_sdk.relational import StructuredSchema
from kb_platform.storage_structured_wire import PortableStructuredClient, PortableStructuredEndpoint

from .application_storage_contracts import JsonDependency
from .knowledge_persistence_contracts import exercise_knowledge_repository


@contextmanager
def structured(root, schema):
    provider = SqlStorageProvider(SQLiteProvider(Path(root) / 'database'))
    space = SpaceKey('wire-contracts', schema.owner, 'synthetic')
    identity = provider.initialize('structured', space)
    store = next(item for item in provider.exports() if item.name == 'structured').open_space(
        space, ExistingDataset(identity.dataset_id))
    endpoint = PortableStructuredEndpoint(store, schemas=(schema,))
    messages = []
    def call(request):
        encoded = json.dumps(request, allow_nan=False)
        messages.append(encoded)
        response = endpoint.execute(json.loads(encoded))
        return json.loads(json.dumps(response, allow_nan=False))
    client = PortableStructuredClient(call, identity=identity)
    try:
        yield client, endpoint, messages
    finally:
        endpoint.close()
        provider.close()


class StructuredWireContracts(unittest.TestCase):
    def test_schema_negotiation_uses_foreign_revision_without_python_digest(self):
        schema = session_schema('sessions')
        with TemporaryDirectory() as directory, structured(directory, schema) as (client, endpoint, _):
            prepares = []

            def foreign(request):
                if request['op'] == 'prepare':
                    prepares.append(request['digest'])
                    self.assertEqual(request['digest'], 'foreign-schema-revision:7')
                    return endpoint.execute({**request, 'digest': schema.digest})
                response = endpoint.execute(request)
                if request['op'] == 'schemas':
                    for advertised in response['schemas']:
                        advertised['digest'] = 'foreign-schema-revision:7'
                        advertised['tables'].reverse()
                        for table in advertised['tables']:
                            table['constraints'].reverse()
                return json.loads(json.dumps(response))

            class NeutralSchema(StructuredSchema):
                @property
                def digest(self):
                    raise AssertionError('Client must not derive a provider schema revision')

            portable = PortableStructuredClient(foreign, identity=client.identity)
            portable.prepare(NeutralSchema(schema.owner, schema.metadata, appendonly=schema.appendonly))
            store = SessionStore(database=portable)
            actor = Actor('tenant', 'owner')
            created = store.create(actor, 'scope', {'provider': 'foreign'})
            self.assertEqual(store.load(actor, created['session_id'])['state'], {'provider': 'foreign'})
            self.assertEqual(prepares, ['foreign-schema-revision:7', 'foreign-schema-revision:7'])

    def test_schema_negotiation_rejects_semantic_or_owner_mismatch_before_prepare(self):
        with TemporaryDirectory() as directory, structured(directory, session_schema('sessions')) as (client, endpoint, _):
            for changed in ('owner', 'columns'):
                with self.subTest(changed=changed):
                    calls = []
                    def foreign(request):
                        calls.append(request['op'])
                        response = endpoint.execute(request)
                        if request['op'] == 'schemas':
                            advertised = response['schemas'][0]
                            advertised['digest'] = 'foreign-revision'
                            if changed == 'owner':
                                advertised['owner'] = 'another-owner'
                            else:
                                advertised['tables'][0]['columns'] = []
                        return response
                    portable = PortableStructuredClient(foreign, identity=client.identity)
                    with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_SCHEMA_UNDECLARED'):
                        portable.prepare(session_schema('sessions'))
                    self.assertEqual(calls, ['schemas'])

    def test_session_state_events_artifacts_replay_and_acl_survive_json_transport(self):
        with TemporaryDirectory() as directory, structured(directory, session_schema('sessions')) as (client, _, messages):
            store = SessionStore(database=client)
            actor = Actor('tenant', 'owner')
            session = store.create(actor, 'scope', {'count': 0})
            sid = session['session_id']
            lease = store.begin(actor, sid, 'request', 0, 'fingerprint')
            store.append(actor, sid, lease['token'], 'message', {'role': 'user', 'content': 'synthetic'}, 'request')
            artifact = store.artifact(actor, sid, lease['token'], {'private': ['evidence']})
            committed = store.finish(actor, sid, lease['token'], {'count': 1}, {'status': 'complete'},
                                     assistant_event={'role': 'assistant', 'content': 'fixture'})
            reopened = SessionStore(database=client)
            self.assertEqual(reopened.load(actor, sid)['state'], {'count': 1})
            self.assertEqual(reopened.read_artifact(actor, sid, artifact['artifact_id']), {'private': ['evidence']})
            self.assertEqual(reopened.begin(actor, sid, 'request', 0, 'fingerprint'), {'cached': committed})
            self.assertEqual([row['kind'] for row in reopened.events(actor, sid)],
                             ['message', 'assistant', 'turn_completed'])
            for denied in (Actor('other-tenant', 'owner'), Actor('tenant', 'other-owner')):
                with self.assertRaises(PlatformError) as error:
                    reopened.load(denied, sid)
                self.assertEqual(error.exception.code, 'SESSION_NOT_FOUND')
            self.assertGreater(len(messages), 20)
            self.assertTrue(all(json.loads(message)['version'] == 1 for message in messages))

    def test_knowledge_ingest_publish_retrieval_acl_and_unknown_requests_use_same_protocol(self):
        with TemporaryDirectory() as directory, structured(directory, knowledge_schema('knowledge')) as (client, _, messages):
            report = exercise_knowledge_repository(self, client, directory)
            self.assertEqual(report['expired_job'], 'UNKNOWN')
            self.assertEqual(report['external_model_calls'], 0)
            self.assertGreater(len(messages), 100)

    def test_rollback_and_shutdown_keep_only_committed_financial_rows(self):
        with TemporaryDirectory() as directory, structured(directory, budget_schema('budget')) as (client, endpoint, _):
            client.prepare(budget_schema('budget'))
            client.seal()
            row = {'id': 'physical-request', 'status': 'UNKNOWN', 'reserved': '{"usd":7}',
                   'usage': None, 'category': 'fixture', 'diagnostic': 'not retried'}
            with client.transaction() as transaction:
                transaction.insert(budget_calls, row)
            with self.assertRaisesRegex(ValueError, 'synthetic failure'):
                with client.transaction() as transaction:
                    transaction.insert(budget_calls, {**row, 'id': 'rolled-back'})
                    raise ValueError('synthetic failure')
            with client.transaction(write=False) as transaction:
                rows = transaction.execute(select(budget_calls)).fetchall()
            self.assertEqual([item['id'] for item in rows], ['physical-request'])
            self.assertEqual(rows[0]['status'], 'UNKNOWN')
            self.assertEqual(rows[0]['reserved'], '{"usd":7}')
            opened = endpoint.execute({'version': 1, 'op': 'begin', 'write': True, 'scope': {},
                                       'limits': asdict(QueryLimits()), 'operation_id': 'abandoned'})
            self.assertTrue(opened['handle'])
            endpoint.close()
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_CLOSED'):
                endpoint.execute({'version': 1, 'op': 'seal'})

    def test_schema_authorization_and_non_json_requests_fail_before_transactions(self):
        with TemporaryDirectory() as directory, structured(directory, session_schema('sessions')) as (client, endpoint, _):
            schemas = endpoint.execute({'version': 1, 'op': 'schemas'})
            self.assertEqual(json.loads(json.dumps(schemas)), schemas)
            selected = schemas['schemas'][0]
            self.assertEqual(selected['owner'], 'sessions')
            self.assertEqual(selected['digest'], session_schema('sessions').digest)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_SCHEMA_UNDECLARED'):
                client.prepare(budget_schema('budget'))
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_JSON_REQUIRED'):
                endpoint.execute({'version': 1, 'op': 'prepare', 'value': object()})
            store = SessionStore(database=client)
            self.assertEqual(store.list_sessions(Actor('tenant', 'owner')), [])

    def test_expired_transaction_rolls_back_and_repeated_finish_observes_same_outcome(self):
        with TemporaryDirectory() as directory, structured(directory, budget_schema('budget')) as (client, endpoint, _):
            client.prepare(budget_schema('budget'))
            client.seal()
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_TRANSACTION_ROLLED_BACK'):
                with client.transaction(limits=QueryLimits(max_seconds=.2), operation_id='expired') as transaction:
                    transaction.insert(budget_calls, {'id': 'expired', 'status': 'UNKNOWN', 'reserved': '{}',
                        'usage': None, 'category': 'fixture', 'diagnostic': None})
                    handle = endpoint._handles[transaction._token]
                    handle._thread.join(timeout=2)
                    self.assertFalse(handle._thread.is_alive())
            repeated = endpoint.execute({'version': 1, 'op': 'finish', 'handle': transaction._token, 'commit': True})
            self.assertEqual(repeated, {'status': 'ROLLED_BACK', 'operation_id': 'expired'})
            with client.transaction(write=False) as transaction:
                self.assertEqual(transaction.execute(select(budget_calls)).fetchall(), [])

    def test_guard_heartbeat_keeps_exclusion_until_explicit_release(self):
        with TemporaryDirectory() as directory, structured(directory, budget_schema('budget')) as (client, endpoint, _):
            client.prepare(budget_schema('budget'))
            client.seal()
            renewed, pings = Event(), []
            original = client._send
            def observe(request):
                result = original(request)
                if request['op'] == 'ping':
                    pings.append(request)
                    if len(pings) >= 4:
                        renewed.set()
                return result
            with patch('kb_platform.storage_structured_wire.GUARD_LEASE_SECONDS', 1), patch.object(client, '_send', observe):
                with client.guard('producer'):
                    self.assertTrue(renewed.wait(4))
                    with self.assertRaisesRegex(DatabaseError, 'DATABASE_GUARD_BUSY'):
                        with client.guard('producer'):
                            self.fail('contended guard must not be entered')
                with client.guard('producer'):
                    self.assertTrue(endpoint._handles)

    def test_commit_acknowledgement_loss_keeps_physical_identity_and_never_replays_write(self):
        for failure in ('PROCESS_RESULT_UNKNOWN', 'PROCESS_EXITED_UNKNOWN', 'invalid-response'):
            with self.subTest(failure=failure), TemporaryDirectory() as directory, \
                    structured(directory, budget_schema('budget')) as (client, _, _):
                client.prepare(budget_schema('budget'))
                client.seal()
                original, finishes = client._send, []
                def lose_acknowledgement(request):
                    response = original(request)
                    if request['op'] == 'finish' and request['commit']:
                        finishes.append(request)
                        if failure == 'invalid-response':
                            return {'status': 'COMMITTED', 'operation_id': 'incorrect'}
                        raise PluginError(failure)
                    return response
                with patch.object(client, '_send', lose_acknowledgement):
                    with self.assertRaises(CommitUnknown) as unknown:
                        with client.transaction(operation_id='physical-request') as transaction:
                            transaction.insert(budget_calls, {'id': 'persisted-once', 'status': 'UNKNOWN',
                                'reserved': '{"usd":7}', 'usage': None, 'category': 'fixture', 'diagnostic': None})
                self.assertEqual(unknown.exception.operation_id, 'physical-request')
                self.assertEqual(len(finishes), 1)
                with client.transaction(write=False) as transaction:
                    rows = transaction.execute(select(budget_calls)).fetchall()
                self.assertEqual([row['id'] for row in rows], ['persisted-once'])
                self.assertEqual(rows[0]['reserved'], '{"usd":7}')

    def test_application_storage_relational_facade_initializes_trusted_session_schema(self):
        with TemporaryDirectory() as directory:
            configuration = initialize_configuration({'version': 2, 'application_id': 'storage-wire-contracts',
                'instances': {'records': {'provider': 'sqlite', 'config': {'root': str(Path(directory) / 'records')}}},
                'services': {'sessions': {'instance': 'records', 'export': 'structured'}},
                'bindings': {'store': {'primary': {'service': 'sessions', 'owner': 'sessions', 'space': 'history'}}}})
            service = ApplicationStorage(configuration, schemas=KNOWLEDGE_SCHEMAS)
            try:
                dependency = JsonDependency(ServiceEndpoint(service, storage_methods()))
                client = StorageClient(dependency, 'store.primary').relational()
                store = SessionStore(database=client)
                actor = Actor('tenant', 'owner')
                created = store.create(actor, 'scope', {'path': 'application-storage'})
                self.assertEqual(store.load(actor, created['session_id'])['state'], {'path': 'application-storage'})
                active = service._structured.pop('store.primary')
                active.close()
                failing = Mock()
                service._structured['store.primary'] = failing
                failing.execute.side_effect = CommitUnknown('physical-request-id')
                with self.assertRaises(CommitUnknown) as unknown:
                    client.prepare(session_schema('sessions'))
                self.assertEqual(unknown.exception.operation_id, 'physical-request-id')
                self.assertEqual(failing.execute.call_count, 1)
                failing.execute.side_effect = DatabaseError('DATABASE_GUARD_BUSY')
                with self.assertRaisesRegex(DatabaseError, 'DATABASE_GUARD_BUSY'):
                    with client.guard('producer'):
                        self.fail('failed acquisition must not enter the guard')
            finally:
                service.close()
