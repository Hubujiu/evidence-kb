from copy import deepcopy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from kb_platform.agent_api import install_agent_routes
from kb_platform.agent_responses import RequestStatusResponse, SessionResponse, TurnResponse
from kb_platform.knowledge_application import KnowledgeApplication
from kb_platform.knowledge_contracts import APPLICATION_OPERATIONS, SESSION_OPERATIONS
from kb_platform.knowledge_schema import knowledge_schema
from kb_platform.knowledge_service import KnowledgeService
from kb_platform.plugin_native import PluginError
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_store import SessionStore

from .knowledge_session_fixture import session_fixture
from . import contract_application_contracts as composition_fixture


class RequestRecoveryContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.actor = Actor('tenant', 'owner')
        self.store, self.authority = session_fixture(temporary.name, self.actor)
        self.addCleanup(self.store.close)
        self.service = object.__new__(KnowledgeService)
        self.service.store = self.store
        self.service.gateway = SimpleNamespace(authorize=Mock(return_value=self.authority),
                                                revalidate=Mock(side_effect=lambda *args: args[2]))
        self.session = self.store.create(self.actor, 'scope', {})
        self.sid = self.session['session_id']
        self.application = KnowledgeApplication(self.service, None, None)
        app = FastAPI()

        @app.exception_handler(PlatformError)
        def platform_error(request: Request, error: PlatformError):
            return JSONResponse({'error': error.code}, status_code=error.status_code)

        install_agent_routes(app, None, None, lambda: self.actor, application=self.application)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def query(self, request='request'):
        return self.client.get(f'/agent/sessions/{self.sid}/requests/{request}')

    def test_unconfirmed_settlement_is_recovered_from_durable_failed_event_before_completion(self):
        begun = self.store.begin(self.actor, self.sid, 'request', 0, 'fingerprint')
        self.store.append(self.actor, self.sid, begun['token'], 'model_failed', {
            'code': 'COMMIT_UNKNOWN', 'model_sent': True, 'physical_request_id': 'physical-one',
            'operation_id': 'unconfirmed-op', 'observed_usage': {'input_tokens': 7, 'output_tokens': 3},
            'status': 200, 'ledger_settlement': 'UNCONFIRMED', 'capture_state': 'COMMITTED',
            'prompt': 'private prompt'}, 'request')
        result = self.query().json()
        self.assertEqual(result['status'], 'reconciliation_required')
        self.assertEqual(result['recovery_action'], 'inspect_operation')
        observed, = result['observations']
        self.assertEqual(observed['operation_id'], 'unconfirmed-op')
        self.assertEqual(observed['provider_http_status'], 200)
        self.assertIs(observed['model_sent'], True)
        self.assertNotIn('private', str(result))
        self.assertEqual(self.store.request_status(self.actor, self.sid, 'request')['status'], 'running')

    def test_cached_completed_event_preserves_usage_without_claiming_new_send(self):
        begun = self.store.begin(self.actor, self.sid, 'request', 0, 'fingerprint')
        self.store.append(self.actor, self.sid, begun['token'], 'model_completed', {
            'usage': {'available': True, 'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            'attempts': 0, 'cache_kind': 'model_response', 'value': 'private result'}, 'request')
        result = self.query().json()
        observed, = result['observations']
        self.assertEqual(observed['observed_usage']['total_tokens'], 15)
        self.assertEqual(observed['cache_kind'], 'model_response')
        self.assertEqual(observed['attempts'], 0)
        self.assertEqual(observed['recovery_action'], 'none')
        self.assertNotIn('model_sent', observed)
        self.assertNotIn('private', str(result))

    def complete(self, *, failure=False):
        begun = self.store.begin(self.actor, self.sid, 'request', 0, 'fingerprint')
        response = {'status': 'error' if failure else 'answered',
            'completion': 'error' if failure else 'complete',
            'answer': '' if failure else 'Authorized original answer', 'claims': [],
            'evidence': [] if failure else [{'citation_id': 'citation', 'text': 'Original'}],
            'unresolved': [], 'stop_reason': 'provider_failure' if failure else 'verified',
            'trace': {'private_path': '/private/receipts', 'prompt': 'private raw prompt',
                'calls': [{'model_sent': True, 'physical_request_id': 'physical-observed',
                    'observed_usage': {'input_tokens': 7, 'output_tokens': 3},
                    'status': 200, 'ledger_settlement': 'UNCONFIRMED', 'capture_state': 'COMMITTED',
                    'budget_operation_id': 'budget-op', 'prompt': 'private raw prompt'}]}}
        if failure:
            return self.store.fail(self.actor, self.sid, begun['token'], self.session['state'], response)
        return self.store.finish(self.actor, self.sid, begun['token'], self.session['state'], response,
                                 authority=self.authority)

    def test_running_expired_and_missing_queries_never_mutate_or_offer_resend(self):
        with patch('kb_platform.session_store.time.time', return_value=100):
            self.store.begin(self.actor, self.sid, 'request', 0, 'fingerprint', lease_seconds=10)
            running = self.query().json()
        self.assertEqual((running['status'], running['recovery_action']), ('running', 'wait'))
        before = self.store.load(self.actor, self.sid)
        with patch('kb_platform.session_store.time.time', return_value=111):
            expired = self.query().json()
        self.assertEqual((expired['status'], expired['send_status'], expired['recovery_action']),
                         ('interrupted', 'unknown', 'inspect_request'))
        missing = self.query('missing').json()
        self.assertEqual((missing['status'], missing['send_status'], missing['recovery_action']),
                         ('not_found', 'unknown', 'verify_request_id'))
        self.assertEqual(self.store.load(self.actor, self.sid), before)
        self.assertEqual(self.store.events(self.actor, self.sid), [])
        with patch('kb_platform.session_store.time.time', return_value=100):
            self.assertEqual(self.query().json()['status'], 'running')

    def test_lost_response_is_retrieved_from_same_authority_after_restart_without_trace(self):
        original = self.complete()
        reopened = SessionStore(database=self.store.database, schema=knowledge_schema, prepared=True)
        self.service.store = reopened
        result = self.query().json()
        self.assertEqual((result['status'], result['recovery_action']), ('completed', 'retrieve_result'))
        self.assertEqual(result['response']['answer'], original['answer'])
        self.assertEqual(result['response']['revision'], original['revision'])
        self.assertNotIn('trace', result['response'])
        self.assertNotIn('private', str(result))
        observed, = result['observations']
        self.assertIs(observed['model_sent'], True)
        self.assertEqual(observed['physical_request_id'], 'physical-observed')
        self.assertEqual(observed['observed_usage'], {'input_tokens': 7, 'output_tokens': 3})
        self.assertEqual(observed['ledger_settlement'], 'UNCONFIRMED')
        self.assertEqual(observed['recovery_action'], 'inspect_operation')
        self.service.gateway.revalidate.assert_called_once()
        self.assertEqual(reopened.begin(self.actor, self.sid, 'request', 0, 'fingerprint'),
                         {'cached': original})

    def test_completed_business_failure_is_returned_as_failure_without_reexecution(self):
        original = self.complete(failure=True)
        result = self.query().json()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['response']['status'], 'error')
        self.assertEqual(result['response']['stop_reason'], 'provider_failure')
        self.assertEqual(self.store.load(self.actor, self.sid)['revision'], original['revision'])

    def test_current_scope_and_owner_authorization_precede_cached_result_read(self):
        self.complete()
        original = self.store.request_status
        self.store.request_status = Mock(wraps=original)
        self.service.gateway.authorize.side_effect = PlatformError('ACCESS_DENIED', 'revoked', 403)
        self.assertEqual(self.query().status_code, 403)
        self.store.request_status.assert_not_called()
        with self.assertRaisesRegex(PlatformError, 'Conversation not available'):
            self.service.request_status(Actor('tenant', 'intruder'), self.sid, 'request')
        with self.assertRaises(PlatformError):
            self.service.request_status(Actor('other-tenant', 'owner'), self.sid, 'request')
        self.store.request_status.assert_not_called()

    def test_revoked_source_redacts_entire_cached_result_but_storage_outage_fails_closed(self):
        self.complete()
        self.service.gateway.revalidate.side_effect = PlatformError('ACCESS_DENIED', 'revoked', 403)
        redacted = self.query().json()
        self.assertEqual(redacted['response'], {'status': 'redacted', 'reason': 'source_unavailable'})
        self.assertEqual(redacted['recovery_action'], 'inspect_request')
        self.assertNotIn('Original', str(redacted))
        self.service.gateway.revalidate.side_effect = PlatformError('STORAGE_UNAVAILABLE', 'outage', 503)
        self.assertEqual(self.query().status_code, 503)

    def test_http_plugin_and_openapi_use_same_required_response_shells(self):
        for operations, names in ((SESSION_OPERATIONS, ('create', 'load', 'begin', 'request_status')),
                                  (APPLICATION_OPERATIONS, ('create', 'get', 'turn', 'request_status'))):
            for name in names:
                with self.subTest(operation=name), self.assertRaises(PluginError):
                    operations[name].method().validate({}, response=True)
        for model in (SessionResponse, TurnResponse, RequestStatusResponse):
            with self.subTest(model=model.__name__), self.assertRaises(ValueError):
                model.model_validate({})
        malformed = deepcopy(self.session)
        malformed['revision'] = True
        with self.assertRaises(ValueError):
            SessionResponse.model_validate(malformed)
        session = self.client.get(f'/agent/sessions/{self.sid}')
        self.assertEqual(session.status_code, 200)
        schema = self.client.get('/openapi.json').json()
        self.assertEqual(schema['components']['schemas']['SessionResponse'], SessionResponse.contract_schema)
        self.assertEqual(schema['components']['schemas']['TurnResponse'], TurnResponse.contract_schema)
        self.assertEqual(schema['components']['schemas']['RequestStatusResponse'],
                         RequestStatusResponse.contract_schema)
        self.application.get = Mock(return_value={})
        with self.assertRaises(ResponseValidationError):
            self.client.get(f'/agent/sessions/{self.sid}')

    def test_begin_discriminates_execution_lease_from_cached_completion(self):
        method = SESSION_OPERATIONS['begin'].method()
        running = self.store.begin(self.actor, self.sid, 'request', 0, 'fingerprint')
        method.validate(running, response=True)
        with self.assertRaises(PluginError):
            method.validate({**running, 'cached': {}}, response=True)
        with self.assertRaises(PluginError):
            method.validate({'cached': {}}, response=True)


class RequestRecoveryAuthorityContracts(unittest.TestCase):
    def test_http_recovery_rechecks_real_source_reader_provider_and_scope_authority(self):
        fixture = composition_fixture.ContractApplicationContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        composition = fixture.compose()
        self.assertEqual(composition.application.model_status()['provider_id'], 'synthetic')
        scope, source = fixture.publish(composition)
        reader = Actor(fixture.actor.tenant_id, 'reader')
        composition.platform.set_member(fixture.actor, scope, reader.principal_id, ['kb.read'])
        composition.platform.set_source_readers(fixture.actor, source['source_id'], [reader.principal_id])
        app = FastAPI()

        @app.exception_handler(PlatformError)
        def platform_error(request: Request, error: PlatformError):
            return JSONResponse({'error': error.code}, status_code=error.status_code)

        install_agent_routes(app, None, None, lambda: reader, application=composition.application)
        with TestClient(app) as client:
            created = client.post(f'/scopes/{scope}/sessions', json={})
            self.assertEqual(created.status_code, 201, created.text)
            sid = created.json()['session_id']
            request = {'message': 'What is the original rule?', 'request_id': 'recover-real-authority',
                       'expected_revision': 0}
            answered = client.post(f'/agent/sessions/{sid}/turns', json=request)
            self.assertEqual(answered.status_code, 200, answered.text)
            self.assertEqual(answered.json()['status'], 'answered', answered.text)
            path = f'/agent/sessions/{sid}/requests/{request["request_id"]}'
            recovered = client.get(path)
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()['response']['answer'], answered.json()['answer'])
            self.assertNotIn('trace', recovered.json()['response'])
            self.assertEqual(len(fixture.physical), 4)
            for policy in ({'llm_context': True, 'allowed_providers': ['different-provider']},
                           {'llm_context': False, 'allowed_providers': ['synthetic']}):
                with self.subTest(policy=policy):
                    composition.platform.set_source_policy(fixture.actor, source['source_id'], policy)
                    redacted = client.get(path)
                    self.assertEqual(redacted.status_code, 200, redacted.text)
                    self.assertEqual(redacted.json()['response']['status'], 'redacted')
                    self.assertNotIn('Original rule value 42.', redacted.text)
            composition.platform.set_source_policy(fixture.actor, source['source_id'],
                {'llm_context': True, 'allowed_providers': ['synthetic']})
            self.assertEqual(client.get(path).json()['response']['status'], 'answered')
            composition.platform.set_source_readers(fixture.actor, source['source_id'], [])
            self.assertEqual(client.get(path).json()['response']['status'], 'redacted')
            composition.platform.set_member(fixture.actor, scope, reader.principal_id, [])
            self.assertIn(client.get(path).status_code, (403, 404))
            self.assertEqual(len(fixture.physical), 4)
            self.assertEqual(composition.application.model_status()['budget']['calls'], 4)
