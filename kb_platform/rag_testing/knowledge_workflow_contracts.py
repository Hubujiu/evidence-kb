import json
import unittest

from kb_platform.core import PlatformError
from . import agent_graph_contracts as graph


class KnowledgeWorkflowContracts(unittest.TestCase):
    setup_service = graph.AgentGraphContracts.setup_service
    turn = graph.AgentGraphContracts.turn
    events = graph.AgentGraphContracts.events

    def failed_tool_turn(self, *, after_evidence=False):
        script = [('decision', graph.decision())]
        if after_evidence:
            script.append(('retrieval', graph.decision(query='second private query')))
        case = self.setup_service(script, policy={'mode': 'LOCKED', 'source_ids': ['s0']})
        original = case.authority.backend.search
        calls = []

        def search(*args, **kwargs):
            calls.append(1)
            if after_evidence and len(calls) == 1:
                return original(*args, **kwargs)
            raise PlatformError('INVALID_SOURCE', 'synthetic failure', 400)

        case.authority.backend.search = search
        result = self.turn(case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'tool_failure')
        self.assertEqual(len(calls), 2 if after_evidence else 1)
        self.assertEqual(len(case.model.calls), 2 if after_evidence else 1)
        return case, result, calls

    def test_tool_failure_response_has_no_answer_claims_or_evidence(self):
        for after_evidence in (False, True):
            with self.subTest(after_evidence=after_evidence):
                _, result, _ = self.failed_tool_turn(after_evidence=after_evidence)
                self.assertEqual(result['answer'], '')
                self.assertEqual(result['claims'], [])
                self.assertEqual(result['evidence'], [])
                self.assertEqual(result['completion'], 'error')

    def test_tool_failure_is_journaled_as_failure_not_assistant_answer(self):
        case, result, _ = self.failed_tool_turn(after_evidence=True)
        events = self.events(case)
        self.assertEqual([e['payload']['code'] for e in events if e['kind'] == 'turn_failed'],
                         ['tool_failure'])
        self.assertFalse([e for e in events if e['kind'] == 'assistant'])
        self.assertEqual([e['payload'] for e in events if e['kind'] == 'turn_completed'], [result])
        receipts = [e['payload']['status'] for e in events if e['kind'] == 'tool_result']
        self.assertEqual(receipts, ['ok', 'error'])

    def test_tool_failure_trace_redacts_derived_data_but_retains_receipts(self):
        _, result, _ = self.failed_tool_turn(after_evidence=True)
        self.assertNotIn('Original rule: value 42.', json.dumps(result))
        self.assertNotIn('second private query', json.dumps(result['trace']['tool_calls']))
        self.assertEqual([row['status'] for row in result['trace']['tool_calls']], ['ok', 'error'])
        self.assertEqual(result['trace']['tool_calls'][-1]['error'], 'INVALID_SOURCE')
        for row in result['trace']['tool_calls']:
            self.assertNotIn('args', row)
            self.assertNotIn('result', row)

    def test_failed_request_replay_preserves_redaction_without_reexecuting(self):
        case, result, calls = self.failed_tool_turn(after_evidence=True)
        replay = self.turn(case)
        self.assertEqual(replay['status'], result['status'])
        self.assertEqual(replay['stop_reason'], result['stop_reason'])
        self.assertEqual(replay['revision'], result['revision'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(case.model.calls), 2)
        self.assertEqual(replay['answer'], '')
        self.assertEqual(replay['evidence'], [])
