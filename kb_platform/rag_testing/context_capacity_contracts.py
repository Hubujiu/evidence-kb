import json
import unittest
from dataclasses import asdict

from kb_platform.agent_context import ContextManager, ContextPolicy
from kb_platform.context_budget import ContextBudgetError, ContextCapacity, RequestBudget, estimate_messages
from kb_platform.agent_api import TurnRequest
from . import model_operation_contracts as model_fixtures
from . import agent_graph_contracts as fixtures


class ContextCapacityContracts(unittest.TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn
    model_case = model_fixtures.ModelOperationContracts.model_case

    def test_unicode_measurement_is_exact_and_not_utf8_token_estimate(self):
        messages = [{'role': 'user', 'content': '中文😀' * 300}]
        budget = RequestBudget(1000)
        self.assertEqual(budget.measure_messages(messages), len(json.dumps(messages, ensure_ascii=False)))
        self.assertLess(budget.measure_messages(messages), estimate_messages(messages)['tokens'])
        capacity = ContextCapacity(ContextPolicy(input_budget=12000), asdict(budget))
        capacity.validate(messages)
        with self.assertRaises(ContextBudgetError):
            ContextCapacity(ContextPolicy(input_budget=100), asdict(budget)).validate(messages)

    def test_unsupported_or_ambiguous_budget_is_rejected(self):
        for value in ({'limit': True, 'unit': 'unicode_codepoints'}, {'limit': 0, 'unit': 'unicode_codepoints'},
                      {'limit': 100, 'unit': 'tokens'}, {'limit': 100},
                      {'limit': 100, 'unit': 'unicode_codepoints', 'extra': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RequestBudget.parse(value)
        with self.assertRaises(ValueError):
            TurnRequest(message='question', request_id='id', expected_revision=0,
                        context_budget={'limit': True, 'unit': 'unicode_codepoints'})

    def test_context_and_rerank_assembly_obey_both_limits_without_mutating_global_policy(self):
        policy = ContextPolicy(input_budget=24000)
        manager = ContextManager(policy)
        evidence = [{'citation_id': 'c', 'source_id': 's', 'text': '中文😀 original fact. ' * 1500}]
        budget = {'limit': 2200, 'unit': 'unicode_codepoints'}
        for prepared in (
            manager.prepare('answer', 'Use originals.', {}, [], evidence, 'fact?', request_budget=budget),
            manager.prepare_rerank('Rank originals.', evidence,
                {'query': 'fact?', 'documents': [{'ref': 'R001', 'text': evidence[0]['text']}]},
                request_budget=budget),
        ):
            self.assertLessEqual(RequestBudget.parse(budget).measure_messages(prepared.messages), 2200)
            self.assertLessEqual(estimate_messages(prepared.messages)['tokens'], policy.effective_input_budget)
            self.assertTrue(prepared.projection['evidence'][0]['text'])
            self.assertEqual(prepared.budget['request_budget']['unit'], 'unicode_codepoints')
        self.assertEqual(manager.policy, policy)
        with self.assertRaises(ContextBudgetError):
            manager.prepare('answer', 'X' * 2300, {}, [], [], 'fact?', request_budget=budget)

    def test_actual_turn_carries_budget_to_every_model_request_and_binds_idempotency(self):
        run = self.model_case([fixtures.decision(), fixtures.decision('answer'),
                              fixtures.cited_answer, fixtures.verdict()], through_contract=True)
        budget = {'limit': 16000, 'unit': 'unicode_codepoints'}
        args = dict(message='What is the original rule?', request_id='budget-turn',
                    expected_revision=0, context_budget=budget)
        result = run.case.service.turn(run.case.authority.actor, run.case.session, **args)
        self.assertEqual(result['status'], 'answered', result)
        for sent in run.sent:
            RequestBudget.parse(budget).validate_messages(sent['messages'])
        self.assertEqual(result['trace']['budget']['request_budget']['limit'], 16000)
        repeat = run.case.service.turn(run.case.authority.actor, run.case.session, **args)
        self.assertEqual(repeat, json.loads(json.dumps(result)))
        self.assertEqual(len(run.sent), 4)
        with self.assertRaises(Exception) as caught:
            run.case.service.turn(run.case.authority.actor, run.case.session,
                                  **{**args, 'context_budget': {**budget, 'limit': 15000}})
        self.assertEqual(caught.exception.code, 'REQUEST_ID_REUSED')
        self.assertEqual(len(run.sent), 4)

    def test_evidence_scope_counts_exact_wire_array_without_charging_system_instructions(self):
        messages = [{'role': 'system', 'content': 'X' * 10000},
                    {'role': 'user', 'content': json.dumps({'evidence': [{'text': '中文😀'}]}, ensure_ascii=False)}]
        budget = RequestBudget(100, scope='evidence')
        self.assertEqual(budget.measure_messages(messages), len('[{"text":"中文😀"}]'))
        ContextCapacity(ContextPolicy(input_budget=24000), asdict(budget)).validate(messages)
        with self.assertRaises(ContextBudgetError):
            RequestBudget(100).validate_messages(messages)
        with self.assertRaises(ContextBudgetError):
            budget.validate_messages([{'role': 'user', 'content': '{}'}])
        manager = ContextManager(ContextPolicy(input_budget=24000))
        prepared = manager.prepare('answer', 'X' * 10000, {}, [],
            [{'citation_id': 'c', 'source_id': 's', 'text': '原文' * 2000}], 'fact?',
            request_budget={'limit': 800, 'unit': 'unicode_codepoints', 'scope': 'evidence'})
        self.assertLessEqual(RequestBudget(800, scope='evidence').measure_messages(prepared.messages), 800)
        self.assertGreater(RequestBudget(800).measure_messages(prepared.messages), 800)

    def test_large_metadata_externalizes_before_rejecting_evidence_budget(self):
        evidence = [{'citation_id': 'c', 'source_id': 's', 'source_version_id': 'v',
                     'parse_snapshot_id': 'p', 'original_sha256': 'hash', 'build_id': 'b',
                     'parse_text_start': 0, 'parse_text_end': 14,
                     'diagnostic_metadata': 'X' * 5000, 'text': 'Original fact.'}]
        prepared = ContextManager(ContextPolicy(input_budget=24000)).prepare(
            'answer', 'Use originals.', {}, [], evidence, 'fact?',
            request_budget={'limit': 800, 'unit': 'unicode_codepoints', 'scope': 'evidence'})
        row, = prepared.projection['evidence']
        for key in ('citation_id', 'source_id', 'source_version_id', 'parse_snapshot_id',
                    'original_sha256', 'build_id', 'parse_text_start', 'parse_text_end'):
            self.assertEqual(row[key], evidence[0][key])
        self.assertEqual(row['text'], 'Original fact.')
        self.assertTrue(row['metadata_reference']['externalized'])
        self.assertEqual(evidence[0]['diagnostic_metadata'], 'X' * 5000)
        self.assertLessEqual(RequestBudget(800, scope='evidence').measure_messages(prepared.messages), 800)

    def test_model_rechecks_explicit_limit_before_transport(self):
        run = self.model_case([], through_contract=True)
        with self.assertRaises(Exception):
            run.case.service.model.complete([{'role': 'user', 'content': 'X' * 2000}],
                role='answer', schema={'type': 'object'},
                call_context={'context_budget': {'limit': 100, 'unit': 'unicode_codepoints'}})
        self.assertEqual(run.sent, [])
        self.assertEqual(run.physical.summary()['physical_requests'], 0)


if __name__ == '__main__':
    unittest.main()
