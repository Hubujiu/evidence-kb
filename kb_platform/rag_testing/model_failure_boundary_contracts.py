import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform.database_contracts import CommitUnknown
from kb_platform.llm_contract import LlmClient, LlmEndpoint, METHODS
from kb_platform.model_adapters import LunaAdapter
from kb_platform.model_errors import ModelPreflightError, RemoteModelError
from kb_platform.model_sdk import Capability, ModelRequest
from kb_platform.model_service import ModelService
from kb_platform.plugin_contracts import ServiceEndpoint

from . import luna_sdk_contracts


class ModelFailureBoundaryContracts(unittest.TestCase):
    def service(self, *, model_cap=0):
        state = luna_sdk_contracts.LunaSDKContracts.setup_provider(self, [], caps={
            'model': model_cap, 'input_tokens': 10000, 'output_tokens': 1000,
            'remote_embedding_calls': 0, 'embedding_input_tokens': 0}, retries=0)
        self.addCleanup(state.ledger.close)
        service = ModelService()
        self.addCleanup(service.close)
        service.register('generation', LunaAdapter(state.provider))
        endpoint = LlmEndpoint(service)
        self.addCleanup(endpoint.close)
        public = ServiceEndpoint(endpoint, METHODS)

        def exchange(request):
            response = public.exchange(json.loads(json.dumps(request)))
            return json.loads(json.dumps(response))

        client = LlmClient(SimpleNamespace(catalog=public.catalog, exchange=exchange))
        return state, client

    def assert_no_send(self, state, error):
        self.assertEqual(error.code, 'RAG_PHYSICAL_REQUEST_ADMISSION_FAILED')
        self.assertEqual(error.preflight_code, 'BUDGET_INSUFFICIENT')
        self.assertIs(error.model_sent, False)
        self.assertEqual(state.requests, [])
        summary = state.ledger.summary()
        self.assertEqual(summary['physical_requests'], 0)
        self.assertEqual(summary['pending_or_unknown'], 0)
        self.assertTrue(all(value == 0 for value in summary['effective_usage_or_reserved'].values()))

    def test_luna_public_invocation_preserves_known_unsent_budget_refusal(self):
        state, client = self.service()
        with self.assertRaises(ModelPreflightError) as caught:
            client.invoke('generation', ModelRequest(Capability.CHAT, {
                'messages': [{'role': 'user', 'content': 'Return a value.'}], 'max_tokens': 16}))
        self.assert_no_send(state, caught.exception)

    def test_luna_public_completion_refusal_releases_global_and_turn_reservations(self):
        state, client = self.service()
        model = client.structured('generation')
        with self.assertRaises(ModelPreflightError) as caught:
            model.complete([{'role': 'user', 'content': 'Return a JSON value.'}], role='answer',
                schema={'type': 'object'}, output_limit=16,
                call_context={'session_id': 'budget-session', 'turn_id': 'budget-turn',
                              'max_physical_calls': 1})
        self.assert_no_send(state, caught.exception)
        self.assertEqual(caught.exception.call_record['attempts'], 0)
        self.assertFalse(caught.exception.call_record['usage']['available'])
        for budget in (model.ledger.snapshot(), model.turn_budget_snapshot('budget-session', 'budget-turn')):
            with self.subTest(budget=budget):
                self.assertIsNotNone(budget)
                for field in ('calls', 'tokens', 'accounted_tokens', 'unknown_usage_calls',
                              'reserved_calls', 'reserved_tokens'):
                    self.assertEqual(budget[field], 0, (field, budget))

    def test_luna_commit_acknowledgement_loss_retains_reservation_without_claiming_model_send(self):
        state, client = self.service(model_cap=2)
        model = client.structured('generation')
        registered = []
        reserve = state.ledger.reserve

        def lose_acknowledgement(request_id, resources, *args, **kwargs):
            reserve(request_id, resources, *args, **kwargs)
            registered.append(request_id)
            raise CommitUnknown('committed-reservation') from OSError('fixture commit acknowledgement lost')

        options = {'role': 'answer', 'schema': {'type': 'object'}, 'output_limit': 16,
                   'call_context': {'session_id': 'commit-session', 'turn_id': 'commit-turn',
                                    'max_physical_calls': 1}}
        messages = [{'role': 'user', 'content': 'Return a JSON value.'}]
        with patch.object(state.ledger, 'reserve', side_effect=lose_acknowledgement):
            with self.assertRaises(RemoteModelError) as caught:
                model.complete(messages, **options)
        error = caught.exception
        self.assertEqual(error.code, 'COMMIT_UNKNOWN')
        self.assertIs(error.model_sent, False)
        self.assertEqual(error.call_record['attempts'], 0)
        self.assertEqual(state.requests, [])
        self.assertEqual(state.provider._issued_requests, 0)
        self.assertEqual(len(registered), 1)
        entry = state.ledger.entry(registered[0])
        self.assertEqual(entry['status'], 'STARTED')
        self.assertEqual(entry['reserved']['model'], 1)
        self.assertIsNone(entry['usage'])
        before = state.ledger.summary()
        self.assertEqual(before['physical_requests'], 1)
        self.assertEqual(before['pending_or_unknown'], 1)
        receipts = luna_sdk_contracts.LunaSDKContracts.receipts(self, state)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['status'], 'ERROR')
        with self.assertRaises(ModelPreflightError) as blocked:
            model.complete(messages, **options)
        self.assertEqual(blocked.exception.preflight_code, 'UNRESOLVED_PHYSICAL_REQUEST')
        self.assertIs(blocked.exception.model_sent, False)
        self.assertEqual(blocked.exception.call_record['attempts'], 0)
        self.assertEqual(state.requests, [])
        self.assertEqual(state.provider._issued_requests, 0)
        self.assertEqual(state.ledger.summary(), before)
        for budget in (model.ledger.snapshot(), model.turn_budget_snapshot('commit-session', 'commit-turn')):
            for field in ('calls', 'tokens', 'accounted_tokens', 'unknown_usage_calls',
                          'reserved_calls', 'reserved_tokens'):
                self.assertEqual(budget[field], 0, (field, budget))

    def test_luna_unsent_commit_failure_keeps_original_storage_error_and_cause(self):
        state, _ = self.service(model_cap=2)
        reserve = state.ledger.reserve
        storage_error = CommitUnknown('committed-reservation')
        connection_error = OSError('fixture commit acknowledgement lost')
        registered = []

        def lose_acknowledgement(request_id, resources, *args, **kwargs):
            reserve(request_id, resources, *args, **kwargs)
            registered.append(request_id)
            raise storage_error from connection_error

        trace = {'request_id': 'direct-sdk-commit-failure', 'attempt': 0}
        with patch.object(state.ledger, 'reserve', side_effect=lose_acknowledgement):
            with self.assertRaises(CommitUnknown) as caught:
                luna_sdk_contracts.LunaSDKContracts.call(self, state, trace=trace)
        self.assertIs(caught.exception, storage_error)
        self.assertIs(caught.exception.__cause__, connection_error)
        self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(trace['attempt'], 0)
        self.assertEqual(state.requests, [])
        self.assertEqual(state.provider._issued_requests, 0)
        self.assertEqual(len(registered), 1)
        self.assertEqual(state.ledger.entry(registered[0])['status'], 'STARTED')
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 1)
