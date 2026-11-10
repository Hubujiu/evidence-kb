from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import httpx

from kb_platform.database_contracts import CommitUnknown
from kb_platform.model_errors import ModelPreflightError, error_details, model_error
from kb_platform.agent_model import BudgetLedger as AgentBudget
from kb_platform.model_http import AnthropicAdapter, OpenAICompatibleAdapter
from kb_platform.model_sdk import Capability, ModelRequest
from kb_platform.model_service import ModelService
from .budget import BudgetLedger


USAGE = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
REQUEST = ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': 'Synthetic fixture'}]},
                       {'request_id': 'logical-fixture', 'session_id': 'session-fixture'})


class ModelDeliveryContracts(unittest.TestCase):
    def fixture(self, root, adapter_type, respond):
        ledger = BudgetLedger(root / 'physical.sqlite3', {'requests': 3})
        self.addCleanup(ledger.close)
        sent = []

        def transport(request):
            sent.append(request)
            return respond(request)

        client = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(client.close)
        adapter = adapter_type(provider='synthetic', model='synthetic', endpoint='https://fixture.invalid/v1',
            api_key='synthetic', capabilities=[Capability.CHAT], ledger=ledger,
            reservation={'requests': 1}, client=client)
        return adapter, ledger, sent

    @staticmethod
    def response(adapter_type, status=200):
        body = ({'content': [{'type': 'text', 'text': 'synthetic'}], 'stop_reason': 'end_turn',
                 'usage': {'input_tokens': 10, 'output_tokens': 5}} if adapter_type is AnthropicAdapter else
                {'choices': [{'message': {'content': 'synthetic'}, 'finish_reason': 'stop'}], 'usage': USAGE})
        return httpx.Response(status, json=body, headers={'x-request-id': 'provider-fixture'})

    def test_response_facts_survive_settlement_rollback_and_lost_ack_without_second_finish(self):
        for adapter_type in (OpenAICompatibleAdapter, AnthropicAdapter):
            for committed in (False, True):
                with self.subTest(adapter=adapter_type.__name__, committed=committed), TemporaryDirectory() as folder:
                    adapter, ledger, sent = self.fixture(Path(folder), adapter_type,
                        lambda _: self.response(adapter_type))
                    finish = ledger.finish
                    unknown = CommitUnknown('synthetic-finish-operation')

                    def fail(identity, **options):
                        if committed:
                            finish(identity, **options)
                        raise unknown

                    with patch.object(ledger, 'finish', side_effect=fail) as call:
                        with self.assertRaises(CommitUnknown) as raised:
                            adapter.invoke(REQUEST)
                    self.assertIs(raised.exception, unknown)
                    self.assertEqual(call.call_count, 1)
                    error = model_error(error_details(raised.exception))
                    self.assertIs(error.model_sent, True)
                    self.assertEqual(error.status, 200)
                    self.assertEqual(error.observed_usage, USAGE)
                    self.assertEqual(error.request_id, 'logical-fixture')
                    self.assertEqual(error.provider_request_id, 'provider-fixture')
                    self.assertEqual(error.ledger_settlement, 'UNCONFIRMED')
                    self.assertEqual(error.capture_state, 'COMMITTED')
                    self.assertEqual(error.budget_operation_id, 'synthetic-finish-operation')
                    row = json.loads((adapter.audit_root / (error.physical_request_id + '.json')).read_text())
                    self.assertEqual(row['http_status'], 200)
                    self.assertEqual(row['usage'], USAGE)
                    self.assertEqual(row['ledger_settlement'], 'UNCONFIRMED')
                    self.assertEqual(ledger.entry(error.physical_request_id)['status'],
                                     'COMPLETE' if committed else 'STARTED')
                    self.assertEqual(len(sent), 1)

    def test_capture_failure_after_response_retains_usage_and_settles_exactly_once(self):
        for adapter_type in (OpenAICompatibleAdapter, AnthropicAdapter):
            with self.subTest(adapter=adapter_type.__name__), TemporaryDirectory() as folder:
                root = Path(folder)

                def respond(_):
                    adapter.audit_root.rename(root / 'retained-audit')
                    adapter.audit_root.write_text('synthetic storage fault')
                    return self.response(adapter_type, 429)

                adapter, ledger, sent = self.fixture(root, adapter_type, respond)
                with patch.object(ledger, 'finish', wraps=ledger.finish) as finish:
                    with self.assertRaisesRegex(RuntimeError, 'PROVIDER_LEDGER_ERROR') as raised:
                        adapter.invoke(REQUEST)
                error = raised.exception
                self.assertEqual(finish.call_count, 1)
                self.assertEqual(error.status, 429)
                self.assertEqual(error.observed_usage, USAGE)
                self.assertEqual(error.capture_state, 'FAILED')
                self.assertEqual(error.ledger_settlement, 'COMMITTED')
                self.assertEqual(ledger.entry(error.physical_request_id)['status'], 'ERROR')
                self.assertEqual(len(sent), 1)

    def test_healthy_inflight_request_reports_busy_unsent_and_unknown_remains_blocking(self):
        with TemporaryDirectory() as folder:
            entered, release = Event(), Event()

            def respond(_):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('SYNTHETIC_FIXTURE_TIMEOUT')
                return self.response(OpenAICompatibleAdapter)

            adapter, ledger, sent = self.fixture(Path(folder), OpenAICompatibleAdapter, respond)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(adapter.invoke, REQUEST)
                try:
                    self.assertTrue(entered.wait(10))
                    with self.assertRaises(ModelPreflightError) as blocked:
                        adapter.invoke(REQUEST)
                    self.assertEqual(blocked.exception.code, 'BUDGET_PRODUCER_BUSY')
                    self.assertIs(blocked.exception.model_sent, False)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(ledger.summary()['physical_requests'], 1)
                finally:
                    release.set()
                pending.result(timeout=10)
            ledger.reserve('synthetic-unknown', {'requests': 1})
            ledger.finish('synthetic-unknown', status='UNKNOWN')
            with self.assertRaisesRegex(ModelPreflightError, 'UNRESOLVED_PHYSICAL_REQUEST'):
                adapter.invoke(REQUEST)
            self.assertEqual(ledger.summary()['effective_usage_or_reserved'], {'requests': 2})
            self.assertEqual(len(sent), 1)

    def test_complete_missing_and_partial_usage_survive_public_structured_failure_record(self):
        for adapter_type in (OpenAICompatibleAdapter, AnthropicAdapter):
            for observed in (None, {}, {'prompt_tokens': 10}, USAGE):
                with self.subTest(adapter=adapter_type.__name__, observed=observed), TemporaryDirectory() as folder:
                    response = self.response(adapter_type)
                    body = response.json()
                    if observed is None:
                        body.pop('usage')
                    elif adapter_type is AnthropicAdapter:
                        body['usage'] = {key: observed[normalized] for key, normalized in
                            (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'))
                            if normalized in observed}
                    else:
                        body['usage'] = observed
                    adapter, ledger, sent = self.fixture(Path(folder), adapter_type,
                        lambda _: httpx.Response(200, json=body, headers={'x-request-id': 'provider-fixture'}))
                    service = ModelService()
                    self.addCleanup(service.close)
                    service.register('generation', adapter)
                    structured = service.structured('generation', ledger=AgentBudget(max_calls=2))
                    with patch.object(ledger, 'finish', side_effect=OSError('synthetic settlement fault')):
                        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_LEDGER_ERROR') as raised:
                            structured.complete(REQUEST.payload['messages'], role='answer', schema={'type': 'object'},
                                call_context={'session_id': 's', 'turn_id': 't', 'max_physical_calls': 2})
                    error = raised.exception
                    self.assertEqual(getattr(error, 'observed_usage', None), observed)
                    self.assertEqual(error.call_record['status'], 200)
                    self.assertEqual(error.call_record['provider_request_id'], 'provider-fixture')
                    self.assertEqual(error.call_record['ledger_settlement'], 'UNCONFIRMED')
                    self.assertEqual(error.call_record['capture_state'], 'COMMITTED')
                    self.assertEqual(error.call_record['attempts'], 1)
                    known = observed == USAGE
                    for budget in (structured._model.ledger.snapshot(), structured._model.turn_budget_snapshot('s', 't')):
                        self.assertEqual(budget['calls'], 1)
                        self.assertEqual(budget['tokens'], 15 if known else 0)
                        self.assertEqual(budget['unknown_usage_calls'], int(not known))
                    self.assertEqual(ledger.summary()['pending_or_unknown'], 1)
                    self.assertEqual(len(sent), 1)

    def test_final_capture_error_does_not_replace_original_settlement_unknown(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            adapter, ledger, sent = self.fixture(root, OpenAICompatibleAdapter,
                lambda _: self.response(OpenAICompatibleAdapter))
            failure = CommitUnknown('synthetic-original-operation')

            def finish(*args, **kwargs):
                adapter.audit_root.rename(root / 'retained-audit')
                adapter.audit_root.write_text('synthetic capture fault')
                raise failure

            with patch.object(ledger, 'finish', side_effect=finish) as called:
                with self.assertRaises(CommitUnknown) as raised:
                    adapter.invoke(REQUEST)
            self.assertIs(raised.exception, failure)
            self.assertEqual(failure.capture_state, 'FAILED')
            self.assertEqual(failure.observed_usage, USAGE)
            self.assertEqual(failure.status, 200)
            self.assertIn('Model capture persistence failed: NotADirectoryError', failure.__notes__)
            self.assertEqual(called.call_count, 1)
            self.assertEqual(len(sent), 1)
            self.assertEqual(ledger.summary()['pending_or_unknown'], 1)

    def test_admission_release_failure_retains_received_response_and_usage(self):
        with TemporaryDirectory() as folder:
            adapter, ledger, sent = self.fixture(Path(folder), OpenAICompatibleAdapter,
                lambda _: self.response(OpenAICompatibleAdapter))
            admission = ledger.admission
            failure = OSError('synthetic producer release error')
            nesting = [0]

            @contextmanager
            def fail_release():
                nesting[0] += 1
                try:
                    with admission():
                        yield
                    if nesting[0] == 1:
                        raise failure
                finally:
                    nesting[0] -= 1

            with patch.object(ledger, 'admission', side_effect=fail_release):
                with self.assertRaises(OSError) as raised:
                    adapter.invoke(REQUEST)
            self.assertIs(raised.exception, failure)
            self.assertEqual(failure.status, 200)
            self.assertEqual(failure.observed_usage, USAGE)
            self.assertEqual(failure.ledger_settlement, 'COMMITTED')
            self.assertEqual(failure.capture_state, 'COMMITTED')
            self.assertEqual(ledger.entry(failure.physical_request_id)['status'], 'COMPLETE')
            self.assertEqual(len(sent), 1)
