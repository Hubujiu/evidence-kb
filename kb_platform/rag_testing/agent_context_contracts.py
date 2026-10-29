"""Synthetic history contracts over the real context projection and compactor."""
from copy import deepcopy
import json
import unittest

from kb_platform.agent_context import ContextBudgetError, ContextManager, ContextPolicy


def event(seq, kind, payload, group='turn', **fields):
    return {'seq': seq, 'kind': kind, 'payload': payload, 'group_id': group, **fields}


class AgentContextContracts(unittest.TestCase):
    def prepare(self, history, *, budget=12000, checkpoint=None, evidence=None):
        return ContextManager(ContextPolicy(input_budget=budget)).prepare(
            'answer', 'Use authorized originals.', {}, history, evidence or [],
            'What is the rule?', checkpoint=checkpoint)

    def test_duplicate_and_out_of_order_events_are_rejected(self):
        for seqs in ((1, 1), (2, 1)):
            with self.subTest(sequences=seqs), self.assertRaises(ContextBudgetError) as caught:
                self.prepare([event(seq, 'user', {'text': 'question'}, f'turn-{i}')
                              for i, seq in enumerate(seqs)])
            self.assertEqual(caught.exception.code, 'INVALID_CONTEXT_EVENT_ORDER')

    def test_incomplete_or_ambiguous_tool_groups_are_rejected(self):
        call = event(1, 'tool_call', {'call_id': 'call-a'})
        result = event(2, 'tool_result', {'call_id': 'call-a', 'text': 'original'})
        cases = (
            ('orphan result', [result], 'INVALID_TOOL_BOUNDARY'),
            ('missing result', [call], 'INCOMPLETE_TOOL_GROUP'),
            ('wrong result', [call, event(2, 'tool_result', {'call_id': 'call-b'})],
             'INVALID_TOOL_BOUNDARY'),
            ('duplicate result', [call, result, event(3, 'tool_result', {'call_id': 'call-a'})],
             'INVALID_TOOL_BOUNDARY'),
            ('interleaved group', [call, event(2, 'user', {'text': 'question'}, 'other'),
                                   {**result, 'seq': 3}], 'INTERLEAVED_CONTEXT_GROUP'),
        )
        for label, history, code in cases:
            with self.subTest(case=label), self.assertRaises(ContextBudgetError) as caught:
                self.prepare(history)
            self.assertEqual(caught.exception.code, code)

    def test_private_role_history_never_enters_messages_or_checkpoint(self):
        history = [
            event(1, 'user', {'text': 'PUBLIC-QUESTION ' * 1000}, 'user', agent_role='user'),
            event(2, 'note', {'text': 'PRIVATE-PLANNER-' * 1000}, 'planner', agent_role='planner'),
            event(3, 'note', {'text': 'PRIVATE-VERIFIER-' * 1000, 'agent_role': 'verifier'}, 'verifier'),
            event(4, 'note', {'text': 'PUBLIC-SHARED'}, 'shared', agent_role='shared'),
            event(5, 'note', {'text': 'PUBLIC-ANSWER'}, 'answer', agent_role='answer'),
        ]
        prepared = self.prepare(history, budget=2500)
        self.assertEqual([row['seq'] for row in prepared.projection['history']], [4, 5])
        serialized = json.dumps(prepared.to_dict())
        self.assertNotIn('PRIVATE-PLANNER', serialized)
        self.assertNotIn('PRIVATE-VERIFIER', serialized)
        for text in ('PUBLIC-QUESTION', 'PUBLIC-SHARED', 'PUBLIC-ANSWER'):
            self.assertIn(text, serialized)
        self.assertTrue(prepared.compaction_needed)
        self.assertEqual([row['seq'] for row in prepared.projection['checkpoint']['records']], [1])

    def test_checkpoint_cannot_split_a_tool_call_from_its_result(self):
        history = [event(1, 'tool_call', {'call_id': 'call-a'}),
                   event(2, 'tool_result', {'call_id': 'call-a', 'text': 'original'})]
        checkpoint = ContextManager.compact(history[:1])
        with self.assertRaises(ContextBudgetError) as caught:
            self.prepare(history, checkpoint=checkpoint)
        self.assertEqual(caught.exception.code, 'CHECKPOINT_SPLITS_GROUP')

    def test_budget_compaction_moves_whole_groups_and_keeps_original_evidence(self):
        history = [event(1, 'tool_call', {'call_id': 'old', 'query': 'old'}, 'old'),
                   event(2, 'tool_result', {'call_id': 'old', 'text': 'old result ' * 1000}, 'old'),
                   event(3, 'tool_call', {'call_id': 'recent', 'query': 'rule'}, 'recent'),
                   event(4, 'tool_result', {'call_id': 'recent', 'text': 'Original rule: 42.'}, 'recent')]
        original = deepcopy(history)
        evidence = [{'citation_id': 'rule-42', 'source_id': 'source-original',
                     'source_version_id': 'version-1', 'text': 'Original rule: 42.'}]
        prepared = self.prepare(history, budget=3500, evidence=evidence)
        self.assertTrue(prepared.compaction_needed)
        self.assertLessEqual(prepared.estimate['tokens'], 3500)
        self.assertEqual([row['seq'] for row in prepared.projection['history']], [3, 4])
        self.assertEqual(prepared.projection['omitted_event_refs'], [{'from_seq': 1, 'through_seq': 2}])
        checkpoint = prepared.projection['checkpoint']
        self.assertEqual(checkpoint['external_event_range'], {'from_seq': 1, 'through_seq': 2})
        self.assertFalse(checkpoint['facts_generated'])
        self.assertEqual(history, original)
        sent = json.loads(prepared.messages[-1]['content'])
        self.assertEqual([row['kind'] for row in sent['history']], ['tool_call', 'tool_result'])
        self.assertEqual(sent['evidence'][0]['text'], 'Original rule: 42.')
        self.assertEqual(sent['evidence'][0]['source_version_id'], 'version-1')
        self.assertEqual(evidence[0]['text'], 'Original rule: 42.')

    def test_incremental_checkpoint_preserves_extracts_and_durable_coverage(self):
        first = [event(1, 'user', {'text': 'Original rule: 42.'}, 'first')]
        prior = ContextManager.compact(first)
        preserved = deepcopy(prior)
        later = [event(2, 'user', {'text': 'Exception: only before 2030.'}, 'second')]
        current = ContextManager.compact(first + later, prior)
        self.assertEqual(prior, preserved)
        self.assertEqual(current['external_event_range'], {'from_seq': 1, 'through_seq': 2})
        self.assertEqual([row['seq'] for row in current['records']], [1, 2])
        self.assertEqual([row['excerpt'] for row in current['records']],
                         ['{"text":"Original rule: 42."}', '{"text":"Exception: only before 2030."}'])
        self.assertFalse(current['facts_generated'])
        self.assertEqual(ContextManager.compact(first + later, current), current)
