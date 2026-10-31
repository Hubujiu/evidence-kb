from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import httpx

from kb_platform.agent_model import BudgetLedger as AgentBudget
from kb_platform.context_budget import ContextBudget
from kb_platform.model_http import OpenAICompatibleAdapter
from kb_platform.model_service import Capability, ModelService
from kb_platform.rag_testing.budget import BudgetLedger


class ModelHttpFailureContracts(unittest.TestCase):
    def test_recording_failure_preserves_sent_attempt_and_upper_budget(self):
        self.check_recording_failures(httpx.ReadTimeout, True)

    def test_recording_failure_keeps_proven_unsent_attempt_at_zero(self):
        self.check_recording_failures(httpx.ConnectTimeout, False)

    def test_recording_failure_after_response_preserves_sent_attempt(self):
        self.check_recording_failures(None, True)

    def check_recording_failures(self, transport_error, sent):
        for recording_error in ('audit', 'ledger'):
            with self.subTest(recording_error=recording_error), TemporaryDirectory() as directory:
                root = Path(directory)
                physical = BudgetLedger(root / 'physical.sqlite3', {'requests': 1})
                requests = []
                def respond(request):
                    requests.append(request)
                    if recording_error == 'audit':
                        adapter.audit_root.rename(root / 'retained-audit')
                        adapter.audit_root.write_text('synthetic filesystem fault', encoding='utf-8')
                    else:
                        with closing(sqlite3.connect(physical.path)) as connection, connection:
                            connection.execute("CREATE TRIGGER fail_finish BEFORE UPDATE ON calls "
                                "BEGIN SELECT RAISE(ABORT, 'synthetic ledger fault'); END")
                    if transport_error is not None:
                        raise transport_error('synthetic transport failure', request=request)
                    return httpx.Response(200, json={'choices': [{'message': {'content': '{"answer":"ok"}'},
                        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 2}})
                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    adapter = OpenAICompatibleAdapter(provider='fixture', model='fixed',
                        endpoint='https://fixture.invalid/v1', api_key='synthetic',
                        capabilities=[Capability.CHAT], ledger=physical, reservation={'requests': 1}, client=client)
                    with closing(ModelService()) as models:
                        models.register('chat', adapter)
                        budget = AgentBudget(max_calls=1, max_tokens=4096)
                        structured = models.structured('chat', ledger=budget,
                            policy=ContextBudget(input_budget=1024, model_window=4096, output_reserve=64))
                        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_LEDGER_ERROR') as raised:
                            structured.complete([{'role': 'user', 'content': 'question'}], role='answer',
                                schema={'type': 'object'}, output_limit=16,
                                call_context={'session_id': 's', 'turn_id': 't', 'max_physical_calls': 1})
                        error = raised.exception
                        self.assertIs(error.model_sent, sent)
                        self.assertEqual(error.call_record['attempts'], int(sent))
                        self.assertTrue(error.physical_request_id)
                        self.assertIsInstance(error.__cause__,
                            OSError if recording_error == 'audit' else sqlite3.IntegrityError)
                        for snapshot in (budget.snapshot(), structured._model.turn_budget_snapshot('s', 't')):
                            self.assertEqual(snapshot['calls'], int(sent))
                            self.assertEqual(snapshot['unknown_usage_calls'], int(sent))
                            self.assertEqual(snapshot['reserved_calls'], 0)
                            self.assertEqual(snapshot['reserved_tokens'], 0)
                            self.assertEqual(snapshot['accounted_tokens'] > 0, sent)
                summary = physical.summary()
                pending = recording_error == 'ledger' or transport_error is httpx.ReadTimeout
                self.assertEqual(summary['pending_or_unknown'], int(pending))
                self.assertEqual(summary['effective_usage_or_reserved'], {'requests': int(pending or sent)})
                self.assertEqual(len(requests), 1)
