from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kb_platform.agent_context import ContextManager, ContextPolicy
from kb_platform.evidence_packing import select_window
from .agent_graph_contracts import ScenarioModel, cited_answer, decision, verdict
from .product_fixture import Authority, Gateway, knowledge_service as AgentService


WORKFLOW_CSV = (
    'from_step,to_step,condition_key,condition_value\n'
    'intake,review,licensed,yes\n'
    'intake,return,licensed,no\n'
    'review,__END__,,\n'
)
WORKFLOW_TEXT = (
    'from_step | to_step | condition_key | condition_value\n'
    'intake | review | licensed | yes\n'
    'intake | return | licensed | no\n'
    'review | __END__ |  | '
)


class CompleteWindowModel(ScenarioModel):
    def __init__(self):
        super().__init__([])

    def complete(self, messages, *, role, **kwargs):
        if role == 'decision':
            value = decision(query='licensed intake review')
        elif role == 'retrieval':
            item = json.loads(messages[-1]['content'])['evidence'][0]
            value = (decision('read', query='licensed intake review',
                              citation_ids=[item['citation_id']], offset=0)
                     if item['remaining_text_externalized'] else decision('answer'))
        elif role == 'answer':
            value = cited_answer(messages)
        elif role == 'verifier':
            value = verdict()
        else:
            raise AssertionError(role)
        self.script.append((role, value))
        return super().complete(messages, role=role, **kwargs)


class EvidenceWindowContracts(unittest.TestCase):
    def prepare(self, text, strategy, *, offset=0, budget=12000, question='licensed review rule'):
        policy = ContextPolicy(input_budget=budget, evidence_strategy=strategy)
        prepared = ContextManager(policy).prepare('retrieval', 'Use authorized originals.', {}, [],
            [{'citation_id': 'workflow', 'source_id': 'original', 'source_version_id': 'version-1',
              'text': text, 'context_offset': offset}], question)
        self.assertLessEqual(prepared.estimate['tokens'], policy.effective_input_budget)
        sent = json.loads(prepared.messages[-1]['content'])['evidence'][0]
        self.assertEqual(sent, prepared.projection['evidence'][0])
        self.assertEqual(sent['full_text_sha256'], sha256(text.encode()).hexdigest())
        self.assertEqual(sent['original_characters'], len(text))
        return sent

    def test_complete_workflow_table_preserves_all_141_original_characters(self):
        self.assertEqual(len(WORKFLOW_TEXT), 141)
        for strategy in ('query_blocks', 'coverage_blocks'):
            with self.subTest(strategy=strategy):
                item = self.prepare(WORKFLOW_TEXT, strategy)
                self.assertEqual(item['text'], WORKFLOW_TEXT)
                self.assertEqual(item['text_start'], 0)
                for field in ('text_truncated', 'remaining_text_externalized', 'externalized'):
                    self.assertFalse(item[field], field)

    def test_complete_windows_preserve_whitespace_without_normalization(self):
        for strategy in ('query_blocks', 'coverage_blocks'):
            for text in ('  licensed review rule  ', '\nlicensed review rule\n',
                         '  licensed review\n\n\t\nrule  \n', ' \t\n\n '):
                with self.subTest(strategy=strategy, text=repr(text)):
                    item = self.prepare(text, strategy)
                    self.assertEqual(item['text'], text)
                    self.assertEqual(item['text_start'], 0)
                    self.assertFalse(item['text_truncated'])
                    self.assertFalse(item['remaining_text_externalized'])
                    self.assertFalse(item['externalized'])

    def test_explicit_offset_preserves_complete_suffix_and_reports_missing_prefix(self):
        offset = WORKFLOW_TEXT.index('intake')
        for strategy in ('query_blocks', 'coverage_blocks'):
            with self.subTest(strategy=strategy):
                item = self.prepare(WORKFLOW_TEXT, strategy, offset=offset)
                self.assertEqual(item['text'], WORKFLOW_TEXT[offset:])
                self.assertEqual(item['text_start'], offset)
                self.assertTrue(item['text_truncated'])
                self.assertTrue(item['remaining_text_externalized'])
                self.assertTrue(item['externalized'])

    def test_nonfitting_original_keeps_relevant_contiguous_window_within_budget(self):
        text = 'Unrelated background. ' * 400 + '\n\nlicensed review rule\n\n' + 'Other details. ' * 400
        for strategy in ('query_blocks', 'coverage_blocks'):
            with self.subTest(strategy=strategy):
                item = self.prepare(text, strategy, budget=2000)
                self.assertEqual(item['text'], 'licensed review rule')
                start = item['text_start']
                self.assertEqual(item['text'], text[start:start + len(item['text'])])
                self.assertTrue(item['text_truncated'])
                self.assertTrue(item['remaining_text_externalized'])
                self.assertTrue(item['externalized'])

    def test_complete_interval_still_obeys_character_limit_and_budget_callback(self):
        text = 'licensed review rule\n\nOther detail.  '
        for limit, fits in ((len(text) - 1, lambda start, end: True),
                            (len(text), lambda start, end: end - start <= 20)):
            with self.subTest(limit=limit):
                start, end = select_window(text, 'licensed', 0, fits, max_characters=limit)
                self.assertIn('licensed review rule', text[start:end])
                self.assertLess(end - start, len(text))
                self.assertLessEqual(end - start, limit)
                self.assertTrue(fits(start, end))

    def test_agent_answers_from_complete_table_without_redundant_read(self):
        with TemporaryDirectory() as folder:
            authority = Authority([('Workflow.csv', WORKFLOW_CSV)])
            self.assertEqual(next(iter(authority.records.values()))['text'], WORKFLOW_TEXT)
            gateway = Gateway(authority)
            gateway.authorize = lambda actor, scope: {
                'scope_id': scope, 'manifest_id': 'build', 'authority_revision': authority.revision}
            gateway.sources = lambda actor, scope: [
                {'source_id': row['source_id'], 'doc_id': row['source_id'],
                 'source_version_id': row['source_version_id'], 'title': row['filename']}
                for row in authority.rows]
            model = CompleteWindowModel()
            service = AgentService(None, model, root=Path(folder), gateway=gateway,
                                   retrieval_config={'channels': ['bm25'], 'rerank': False})
            session = service.create(authority.actor, authority.scope)
            result = service.turn(authority.actor, session['session_id'],
                message='From intake with licensed=yes, what is the next step?',
                request_id='complete-workflow', expected_revision=0)
            self.assertEqual(result['status'], 'answered', result['stop_reason'])
            self.assertEqual(result['completion'], 'complete')
            self.assertEqual(result['answer'], WORKFLOW_TEXT)
            self.assertEqual(result['unresolved'], [])
            self.assertEqual(len(result['trace']['tool_calls']), 1)
            self.assertEqual(len(model.calls), 4)


if __name__ == '__main__':
    unittest.main()
