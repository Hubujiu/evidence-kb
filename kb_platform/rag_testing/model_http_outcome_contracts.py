from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from kb_platform.agent_model import BudgetLedger as AgentBudget
from kb_platform.database_contracts import CommitUnknown
from kb_platform.llm_contract import LlmClient, LlmEndpoint
from kb_platform.model_errors import ModelPreflightError, RemoteModelError
from kb_platform.model_http import AnthropicAdapter, OpenAICompatibleAdapter
from kb_platform.model_sdk import Capability, ModelRequest
from kb_platform.model_service import ModelService

from .budget import BudgetLedger


USAGE = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
MESSAGES = [{'role': 'user', 'content': 'Read the local fixture.'}]


class ModelHttpOutcomeContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.serial = 0

    def fixture(self, respond, *, adapter_type=OpenAICompatibleAdapter):
        self.serial += 1
        root = self.root / str(self.serial)
        root.mkdir()
        physical = BudgetLedger(root / 'physical.sqlite3', {'requests': 3})
        self.addCleanup(physical.close)
        sent = []

        def handle(request):
            sent.append(request)
            return respond(request)

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        adapter = adapter_type(provider='fixture', model='fixed', endpoint='https://fixture.invalid/v1',
            api_key='synthetic', capabilities=[Capability.CHAT], ledger=physical,
            reservation={'requests': 1}, client=client)
        service = ModelService()
        service.register('chat', adapter)
        self.addCleanup(service.close)
        endpoint = LlmEndpoint(service)
        self.addCleanup(endpoint.close)
        public = LlmClient(SimpleNamespace(catalog=endpoint.catalog, exchange=endpoint.exchange))
        model = public.structured('chat', ledger=AgentBudget(max_calls=3))
        return SimpleNamespace(root=root, physical=physical, sent=sent, adapter=adapter, model=model)

    def complete(self, state, *, turn='t'):
        return state.model.complete(MESSAGES, role='answer', schema={'type': 'object'}, output_limit=30,
            call_context={'session_id': 's', 'turn_id': turn, 'max_physical_calls': 3})

    def budgets(self, state, turn='t'):
        return state.model.ledger.snapshot(), state.model.turn_budget_snapshot('s', turn)

    def test_unavailable_budget_database_is_unsent_through_public_llm(self):
        state = self.fixture(lambda _: self.fail('budget failure must not send'))
        before = state.physical.summary()
        path = state.physical.path
        retained = path.with_suffix('.retained')
        path.rename(retained)
        path.mkdir()
        try:
            for _ in range(2):
                with self.assertRaises(RemoteModelError) as raised:
                    self.complete(state)
                self.assertEqual(raised.exception.code, 'DATABASE_BACKEND_UNAVAILABLE')
                self.assertIs(raised.exception.model_sent, False)
                self.assertEqual(raised.exception.call_record['attempts'], 0)
            for budget in self.budgets(state):
                self.assertEqual(budget['calls'], 0)
                self.assertEqual(budget['tokens'], 0)
                self.assertEqual(budget['unknown_usage_calls'], 0)
                self.assertEqual(budget['accounted_tokens'], 0)
                self.assertEqual(budget['reserved_calls'], 0)
            self.assertEqual(state.sent, [])
            self.assertEqual(list(state.adapter.audit_root.glob('*.json')), [])
        finally:
            path.rmdir()
            retained.rename(path)
        self.assertEqual(state.physical.summary(), before)

    def test_uncertain_reservation_commit_retains_storage_fact_without_sending(self):
        state = self.fixture(lambda _: self.fail('uncertain admission must not send'))
        reserve = state.physical.reserve
        unknown = CommitUnknown('reservation-commit-control')
        cause = OSError('commit acknowledgement lost')

        def lose_acknowledgement(*args, **kwargs):
            reserve(*args, **kwargs)
            raise unknown from cause

        request = ModelRequest(Capability.CHAT, {'messages': MESSAGES, 'max_tokens': 30})
        with patch.object(state.physical, 'reserve', side_effect=lose_acknowledgement):
            with self.assertRaises(CommitUnknown) as raised:
                state.adapter.invoke(request)
        self.assertIs(raised.exception, unknown)
        self.assertIs(raised.exception.__cause__, cause)
        self.assertEqual(raised.exception.code, 'COMMIT_UNKNOWN')
        self.assertEqual(raised.exception.operation_id, 'reservation-commit-control')
        self.assertIs(raised.exception.model_sent, False)
        before = state.physical.summary()
        self.assertEqual(before['physical_requests'], 1)
        self.assertEqual(before['pending_or_unknown'], 1)
        self.assertEqual(before['effective_usage_or_reserved'], {'requests': 1})
        with self.assertRaises(ModelPreflightError) as blocked:
            state.adapter.invoke(request)
        self.assertEqual(blocked.exception.code, 'UNRESOLVED_PHYSICAL_REQUEST')
        self.assertIs(blocked.exception.model_sent, False)
        self.assertEqual(state.physical.summary(), before)
        self.assertEqual(state.sent, [])

    def test_invalid_content_preserves_observed_usage_and_physical_identity(self):
        responses = [
            (OpenAICompatibleAdapter, {'choices': [{'message': {'content': None}}], 'usage': USAGE}),
            (AnthropicAdapter, {'content': [{'type': 'tool_use'}],
                'usage': {'input_tokens': 10, 'output_tokens': 5}}),
        ]
        for adapter_type, body in responses:
            with self.subTest(adapter=adapter_type.__name__):
                state = self.fixture(lambda _, value=body: httpx.Response(200, json=value),
                                     adapter_type=adapter_type)
                with self.assertRaises(RemoteModelError) as raised:
                    self.complete(state)
                error = raised.exception
                self.assertEqual(error.code, 'INVALID_MODEL_RESPONSE')
                self.assertIs(error.model_sent, True)
                self.assertEqual(error.observed_usage, USAGE)
                self.assertTrue(error.physical_request_id)
                self.assertEqual(error.call_record['attempts'], 1)
                self.assertIs(error.call_record['usage']['available'], True)
                self.assertEqual(error.call_record['usage']['total_tokens'], 15)
                self.assertEqual(len(state.sent), 1)
                row = state.physical.entry(error.physical_request_id)
                self.assertEqual(row['status'], 'COMPLETE')
                self.assertEqual(row['usage'], {'requests': 1})
                for budget in self.budgets(state):
                    self.assertEqual(budget['calls'], 1)
                    self.assertEqual(budget['tokens'], 15)
                    self.assertEqual(budget['unknown_usage_calls'], 0)
                    self.assertEqual(budget['accounted_tokens'], 15)

    def test_invalid_content_without_usage_retains_unknown_usage(self):
        state = self.fixture(lambda _: httpx.Response(200, json={'choices': [{'message': {'content': None}}]}))
        with self.assertRaises(RemoteModelError) as raised:
            self.complete(state)
        error = raised.exception
        self.assertEqual(error.code, 'INVALID_MODEL_RESPONSE')
        self.assertIs(error.model_sent, True)
        self.assertIsNone(getattr(error, 'observed_usage', None))
        self.assertEqual(error.call_record['attempts'], 1)
        self.assertIs(error.call_record['usage']['available'], False)
        self.assertEqual(state.physical.entry(error.physical_request_id)['status'], 'COMPLETE')
        for budget in self.budgets(state):
            self.assertEqual(budget['calls'], 1)
            self.assertEqual(budget['tokens'], 0)
            self.assertEqual(budget['unknown_usage_calls'], 1)
            self.assertGreater(budget['accounted_tokens'], 0)
        self.assertEqual(len(state.sent), 1)

    def test_read_timeout_stays_unknown_and_blocks_another_turn(self):
        def interrupted(request):
            raise httpx.ReadTimeout('response interrupted', request=request)

        state = self.fixture(interrupted)
        with self.assertRaises(RemoteModelError) as raised:
            self.complete(state)
        self.assertEqual(raised.exception.code, 'PROVIDER_OUTCOME_UNKNOWN')
        self.assertIs(raised.exception.model_sent, True)
        self.assertEqual(raised.exception.call_record['attempts'], 1)
        self.assertEqual(state.physical.entry(raised.exception.physical_request_id)['status'], 'UNKNOWN')
        before = state.physical.summary()
        self.assertEqual(before['pending_or_unknown'], 1)
        with self.assertRaises(ModelPreflightError) as blocked:
            self.complete(state, turn='next')
        self.assertEqual(blocked.exception.code, 'UNRESOLVED_PHYSICAL_REQUEST')
        self.assertIs(blocked.exception.model_sent, False)
        self.assertEqual(blocked.exception.call_record['attempts'], 0)
        self.assertEqual(state.model.ledger.snapshot()['calls'], 1)
        self.assertEqual(state.model.ledger.snapshot()['unknown_usage_calls'], 1)
        self.assertEqual(state.model.turn_budget_snapshot('s', 'next')['calls'], 0)
        self.assertEqual(state.physical.summary(), before)
        self.assertEqual(len(state.sent), 1)
