import json
from types import SimpleNamespace
import unittest

import httpx

from kb_platform.agent_luna import LunaModelProvider
from kb_platform.agent_model import AgentModel, BudgetLedger
from . import agent_graph_contracts as fixtures
from .budget import BudgetLedger as PhysicalLedger
from .provider_capture import CapturedLuna, observe_client


def read_decision(messages):
    evidence = json.loads(messages[-1]['content'])['evidence']
    return fixtures.decision('read', citation_ids=[evidence[0]['citation_id']], offset=0)


def malformed_answer_decision(messages):
    evidence = json.loads(messages[-1]['content'])['evidence'][0]
    return {**fixtures.decision('answer'), 'claims': [
        {'text': evidence['text'], 'evidence_ids': [evidence['citation_id']]}],
        'quotes': [{'evidence_id': evidence['citation_id'], 'text': evidence['text']}]}


class ModelOperationContracts(unittest.TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn

    def model_case(self, outputs, *, config=None):
        case = self.setup_service(config=config)
        configuration = case.root / 'provider.json'
        configuration.write_text(json.dumps({'cache_dir': str(case.root / 'cache'),
            'log_path': str(case.root / 'calls.jsonl'), 'luna': {
                'api_key': 'synthetic', 'base_url': 'https://fixture.invalid/v1',
                'model': 'model-operation-fixture', 'retries': 0, 'cache_mode': 'disabled'}}), 'utf-8')
        client = CapturedLuna(configuration)
        self.addCleanup(client.close)
        pending, sent = list(outputs), []

        def respond(request):
            payload = json.loads(request.content)
            sent.append(payload)
            value = pending.pop(0)
            value = value(payload['messages']) if callable(value) else value
            if isinstance(value, BaseException):
                raise value
            return httpx.Response(200, json={'choices': [{'message': {
                'content': value if isinstance(value, str) else json.dumps(value)},
                'finish_reason': 'stop'}], 'usage': {
                    'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})

        physical = PhysicalLedger(case.root / 'physical.sqlite', {
            'model': 100, 'input_tokens': 5_000_000, 'output_tokens': 500_000,
            'remote_embedding_calls': 0, 'embedding_input_tokens': 0})
        observe_client(client, physical, case.root / 'capture',
                       category='model-operation-contract', inner=httpx.MockTransport(respond))
        budget = BudgetLedger(max_calls=100, max_tokens=5_000_000)
        case.service.model = AgentModel(LunaModelProvider(client),
                                       policy=case.service.context_policy, ledger=budget)
        return SimpleNamespace(case=case, pending=pending, sent=sent, budget=budget, physical=physical)

    def test_independent_planner_operations_each_correct_once_with_strict_output_validation(self):
        run = self.model_case([fixtures.decision(),
            'The evidence is available. {"intent":"knowledge","action":"answer"}',
            read_decision, malformed_answer_decision, fixtures.decision('answer'),
            fixtures.cited_answer, fixtures.verdict()])
        result = self.turn(run.case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['answer'], 'Original rule: value 42.')
        failures = [row for row in result['trace']['calls'] if row.get('validator_reason')]
        self.assertEqual([row['validator_reason'] for row in failures],
                         ['INVALID_JSON_SYNTAX', 'AGENT_SCHEMA_MISMATCH'])
        self.assertEqual(failures[1]['validator_issue'], {'path': [], 'rule': 'additionalProperties'})
        repairs = result['trace']['format_repairs']
        self.assertEqual(len(repairs), 2)
        for repair in repairs:
            self.assertEqual(repair['attempts'], 1)
            self.assertNotEqual(repair['failed_call_id'], repair['new_call_id'])
        ids = [row['call_id'] for row in result['trace']['calls']]
        self.assertEqual(len(set(ids)), 7)
        self.assertEqual(run.budget.snapshot()['calls'], 7)
        self.assertEqual(run.physical.summary()['physical_requests'], 7)
        self.assertEqual([row['name'] for row in result['trace']['tool_calls']], ['search', 'read'])
        self.assertFalse(run.pending)

    def test_output_protocol_belongs_to_the_role_and_is_not_duplicated_on_correction(self):
        run = self.model_case([fixtures.decision(), '{', fixtures.decision('answer'),
                              fixtures.cited_answer, fixtures.verdict()])
        result = self.turn(run.case)
        self.assertEqual(result['status'], 'answered', result)
        for call, payload in zip(result['trace']['calls'], run.sent, strict=True):
            system = payload['messages'][0]['content']
            if call['role'] in {'decision', 'retrieval'}:
                self.assertNotIn('claims.evidence_ids', system)
                self.assertNotIn('quotes.evidence_id', system)
                self.assertEqual(system.count('Planner references are call-local short handles'), 1)
            if call['role'] == 'answer':
                self.assertIn('claims.evidence_ids', system)
            if call['role'] == 'verifier':
                self.assertNotIn('Citation protocol:', system)

    def test_two_invalid_responses_in_one_operation_stop_without_executing_a_tool(self):
        run = self.model_case(['prefix {"intent":"knowledge","action":"search"}',
                              {**fixtures.decision(), 'claims': []}, fixtures.decision()])
        result = self.turn(run.case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'INVALID_AGENT_OUTPUT')
        self.assertEqual(len(result['trace']['format_repairs']), 1)
        self.assertEqual(len(run.sent), 2)
        self.assertEqual(run.physical.summary()['physical_requests'], 2)
        self.assertEqual(run.case.authority.calls, [])

    def test_multiple_corrections_preserve_the_turn_budget_and_answer_reservation(self):
        run = self.model_case([fixtures.decision(), '{', read_decision,
                              malformed_answer_decision, fixtures.decision('answer'),
                              fixtures.cited_answer, fixtures.verdict()], config={'max_model_calls': 6})
        result = self.turn(run.case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'AGENT_BUDGET_EXHAUSTED')
        self.assertEqual(len(run.sent), 4)
        self.assertEqual(run.budget.snapshot()['calls'], 4)
        self.assertEqual(run.physical.summary()['physical_requests'], 4)
        self.assertEqual(len(run.pending), 3)
        self.assertNotIn('new_call_id', result['trace']['format_repairs'][-1])

    def test_transport_failure_is_not_an_output_correction(self):
        run = self.model_case([httpx.ReadError('synthetic disconnected response'), fixtures.decision()])
        result = self.turn(run.case)
        self.assertEqual(result['status'], 'error', result)
        self.assertNotIn('format_repairs', result['trace'])
        self.assertEqual(len(run.sent), 1)
        self.assertEqual(len(run.pending), 1)


if __name__ == '__main__':
    unittest.main()
