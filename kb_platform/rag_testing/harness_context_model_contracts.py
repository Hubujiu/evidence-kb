"""Offline context and shared-model contracts using synthetic provider receipts."""
from copy import deepcopy
from dataclasses import asdict
import json
from threading import Event
from types import SimpleNamespace
import unittest

from kb_platform.agent_context import ContextPolicy
from kb_platform.agent_engine import execution_scope
from kb_platform.agent_model import AgentModel, BudgetLedger
from kb_platform.agent_native import KernelError
from kb_platform.agent_provider import ProviderCompletion
from kb_platform.context_budget import ContextBudget, ContextBudgetError, estimate_messages
from kb_platform.harness_context import HistoryContext, RecentTurns, estimate_request
from kb_platform.harness_model import StructuredDecisionModel
from kb_platform.model_errors import RemoteModelError


class _Provider:
    max_attempts = 1

    def __init__(self, values, *, after=None):
        self.values, self.calls, self.records = list(values), [], []
        self.after = after

    def identity(self, messages, output_limit):
        return {'provider': 'synthetic-context-model/1', 'messages': messages, 'output_limit': output_limit}

    def complete(self, messages, output_limit, *, purpose, key, trace):
        trace['attempt'] += 1
        self.calls.append({'messages': deepcopy(messages), 'purpose': purpose, 'trace': deepcopy(trace)})
        if self.after:
            self.after()
        return ProviderCompletion(json.dumps(self.values.pop(0)), 'stop',
                                  {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})

    def cache_get(self, key):
        return None

    def cache_put(self, key, value):
        pass

    def record(self, purpose, key, **metadata):
        self.records.append(metadata)

    def save_failed_response(self, result, metadata):
        return {'synthetic': True}


TOOLS = [{'name': 'sum', 'description': 'Sum two numbers.', 'parameters': {
    'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
    'required': ['a', 'b'], 'additionalProperties': False}}]


class HarnessContextContracts(unittest.TestCase):
    def test_truncation_retains_whole_tool_turn_and_never_changes_history(self):
        old = [{'role': 'user', 'content': 'old ' * 1000}, {'role': 'assistant', 'content': 'old answer'}]
        current = [{'role': 'user', 'content': 'add 2 and 3'},
                   {'role': 'assistant', 'tool_call': {'name': 'sum', 'arguments': {'a': 2, 'b': 3}}},
                   {'role': 'tool', 'name': 'sum', 'content': {'value': 5}}]
        history = old + current
        before = deepcopy(history)
        limit = estimate_request(current, TOOLS)['tokens']
        prepared = HistoryContext(policy=ContextBudget(input_budget=limit)).prepare(history, TOOLS, None)
        self.assertEqual(prepared['messages'], current)
        self.assertEqual(prepared['context_projection']['omitted_turns'], 1)
        self.assertEqual(history, before)
        prepared['messages'][-1]['content']['value'] = 99
        self.assertEqual(history[-1]['content']['value'], 5)

    def test_required_current_turn_system_and_tool_schema_cannot_be_silently_dropped(self):
        messages = [{'role': 'user', 'content': 'hello'}]
        limit = estimate_request(messages, [])['tokens']
        for options, tools in (({'system': 'system instructions ' * 20}, []), ({}, TOOLS)):
            with self.subTest(options=options, tools=tools):
                with self.assertRaisesRegex(ContextBudgetError, 'CONTEXT_REQUIRED_STATE_TOO_LARGE'):
                    HistoryContext(policy=ContextBudget(input_budget=limit), **options).prepare(messages, tools, None)

    def test_orphan_or_pending_tool_pair_is_rejected_before_request(self):
        for suffix in ([{'role': 'tool', 'name': 'sum', 'content': 5}],
                       [{'role': 'assistant', 'tool_call': {'name': 'sum', 'arguments': {}}}],
                       [{'role': 'assistant', 'tool_call': {'name': 'sum', 'arguments': {}}},
                        {'role': 'tool', 'name': 'different', 'content': 5}]):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ContextBudgetError, 'CONTEXT_UNPAIRED_TOOL'):
                HistoryContext().prepare([{'role': 'user', 'content': 'go'}, *suffix], TOOLS, None)

    def test_compaction_is_replaceable_but_cannot_remove_current_turn(self):
        class DropAll:
            def select_start(self, groups, fits):
                return len(groups)

        with self.assertRaisesRegex(ContextBudgetError, 'INVALID_COMPACTION_BOUNDARY'):
            HistoryContext(compaction=DropAll()).prepare([{'role': 'user', 'content': 'required'}], [], None)
        self.assertEqual(RecentTurns().select_start([[1], [2]], lambda start: True), 0)

    def test_knowledge_policy_keeps_original_fields_and_estimation(self):
        policy = ContextPolicy(evidence_strategy='query_blocks')
        expected = asdict(ContextBudget()) | {'evidence_strategy': 'query_blocks'}
        self.assertEqual(asdict(policy), expected)
        with self.assertRaisesRegex(ValueError, 'INVALID_CONTEXT_POLICY'):
            ContextPolicy(input_budget=True)
        with self.assertRaisesRegex(ContextBudgetError, 'TOKEN_CAPABILITY_UNVERIFIED'):
            HistoryContext(policy=ContextBudget(strict_tokens=True)).prepare(
                [{'role': 'user', 'content': 'hello'}], [], None)


class HarnessModelContracts(unittest.TestCase):
    def test_context_estimate_matches_actual_metered_structured_request(self):
        provider = _Provider([{'kind': 'tool', 'name': 'sum', 'arguments': {'a': 2, 'b': 3}}])
        adapter = StructuredDecisionModel(AgentModel(provider))
        context = HistoryContext(policy=adapter.policy, measure=adapter.estimate_request)
        prepared = context.prepare([{'role': 'user', 'content': 'Add 2 and 3.'}], TOOLS, None)
        result = adapter.decide(prepared, SimpleNamespace(session_id='session', turn_id='turn'))
        self.assertEqual(result, {'kind': 'tool', 'name': 'sum', 'arguments': {'a': 2, 'b': 3}})
        self.assertEqual(prepared['context_projection']['estimate'], estimate_messages(provider.calls[0]['messages']))
        self.assertEqual(adapter.model.ledger.snapshot()['calls'], 1)
        self.assertEqual(provider.calls[0]['trace']['session_id'], 'session')

    def test_auxiliary_completion_and_loop_share_budget_and_receipts(self):
        provider = _Provider([{'kind': 'answer', 'value': 'ok'}, {'summary': 'short'}])
        model = AgentModel(provider, ledger=BudgetLedger(max_calls=2))
        adapter = StructuredDecisionModel(model)
        self.assertEqual(adapter.decide({'messages': [{'role': 'user', 'content': 'hello'}], 'tools': []},
                         SimpleNamespace(session_id='session', turn_id='turn'))['value'], 'ok')
        options = {'role': 'summary', 'schema': {'type': 'object', 'properties': {'summary': {'type': 'string'}},
                   'required': ['summary'], 'additionalProperties': False},
                   'call_context': {'session_id': 'session', 'turn_id': 'turn', 'call_id': 'summary-1'}}
        envelope = adapter.complete([{'role': 'user', 'content': 'Summarize.'}], **options)
        self.assertEqual(envelope.value, {'summary': 'short'})
        self.assertEqual(envelope.attempts, 1)
        with self.assertRaisesRegex(RemoteModelError, 'AGENT_CALL_BUDGET_EXHAUSTED'):
            adapter.complete([{'role': 'user', 'content': 'Another.'}], **options)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([row['purpose'] for row in provider.calls], ['agent_decision', 'agent_summary'])
        self.assertEqual(model.ledger.snapshot()['calls'], 2)

    def test_invalid_tool_arguments_fail_before_native_dispatch_and_remain_charged(self):
        provider = _Provider([{'kind': 'tool', 'name': 'sum', 'arguments': {'a': 'wrong', 'b': 2}}])
        adapter = StructuredDecisionModel(AgentModel(provider))
        with self.assertRaisesRegex(RemoteModelError, 'INVALID_AGENT_OUTPUT'):
            adapter.decide({'messages': [{'role': 'user', 'content': 'go'}], 'tools': TOOLS},
                           SimpleNamespace(session_id='session', turn_id='turn'))
        self.assertEqual(adapter.model.ledger.snapshot()['calls'], 1)

    def test_independent_call_does_not_return_success_after_cancellation(self):
        cancellation = Event()
        provider = _Provider([{'summary': 'late'}], after=cancellation.set)
        adapter = StructuredDecisionModel(AgentModel(provider))
        with execution_scope(deadline_seconds=10, cancellation=cancellation):
            with self.assertRaisesRegex(KernelError, 'AGENT_CANCELLED'):
                adapter.complete([{'role': 'user', 'content': 'summarize'}], role='summary',
                    schema={'type': 'object'}, call_context={'call_id': 'cancelled'})
        self.assertEqual(len(provider.calls), 1)
        self.assertIn('deadline_monotonic', provider.calls[0]['trace'])
        self.assertEqual(adapter.model.ledger.snapshot()['calls'], 1)


if __name__ == '__main__':
    unittest.main()
