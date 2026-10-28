"""Offline contracts for exact evidence leaves across tool messages."""
import json
import unittest

from .answer_metrics import ordered_numeric_answer_score
from .contracts import (AdapterInput, Case, Evidence, Fragment, Plan, RequestEvidenceBinding,
                        Source, Suite, Trace, digest, text_hash)
from .scoring import corpus_hash, score_case


class NumericAnswerContracts(unittest.TestCase):
    def test_numeric_order_sign_decimal_and_date_are_distinguished(self):
        for actual, expected, match in (
            ('55|1', '5.5|1', 0), ('5.5|1', '5.5|-1', 0), ('12|3', '1|23', 0),
            ('5.50|2023-07', '5.5|2023-07', 1), ('NaN', '1', 0),
        ):
            with self.subTest(actual=actual, expected=expected):
                self.assertEqual(ordered_numeric_answer_score(actual, expected)['exact_match'], match)


class RequestProjectionContracts(unittest.TestCase):
    def setUp(self):
        source = Source(id='doc-p1', kb='doc', version='1', text='value 42', sha256=text_hash('value 42'))
        self.case = Case(input=AdapterInput(
            case_id='one', question='value?', kb='doc', principal='public', acl_revision='1',
            allowed_sources=(source.id,), as_of='2026-10-28', context_budget=2000),
            family='one', any_of=(('e',),))
        self.suite = Suite(id='projection-control', label_provenance='synthetic_engineering',
            sources=(source,), evidence=(Evidence(id='e', source_id=source.id, start=0, end=8,
                                                 quote=source.text, fact='42'),), cases=(self.case,))
        fragment = Fragment(source_id=source.id, source_sha256=source.sha256, version=source.version,
                            kb=source.kb, start=0, end=8, text=source.text)
        self.messages = ({'role': 'user', 'content': 'question'}, {'role': 'tool', 'content':
            json.dumps({'data': {'content': [{'page': 1, 'text': source.text}]}})})
        self.binding = RequestEvidenceBinding(message_index=1, json_path=('data', 'content', 0, 'text'),
                                              fragment_index=0)
        self.trace = Trace(case_id='one', corpus_hash=corpus_hash(self.suite), implementation='control',
            principal='public', acl_revision='1', stages={'retrieval': (fragment,)}, final=(fragment,),
            final_context=json.dumps([{'source_id': source.id, 'text': source.text}]),
            request_messages=self.messages, request_evidence_bindings=(self.binding,), capture='pre_send')
        self.plan = Plan(suite_hash=digest(self.suite), mode='live', required_stages=('retrieval',))

    def test_exact_tool_evidence_leaf_is_accepted(self):
        self.assertEqual(score_case(self.suite, self.case, self.trace, self.plan)['outcome'], 'PASS')

    def test_nested_json_tool_evidence_leaf_is_accepted(self):
        nested = self.trace.model_copy(update={
            'request_messages': (self.messages[0], {'role': 'tool', 'content': [
                {'type': 'text', 'text': self.messages[1]['content']}]}),
            'request_evidence_bindings': (self.binding.model_copy(update={
                'json_path': (0, 'text', '$json', 'data', 'content', 0, 'text')}),)})
        self.assertEqual(score_case(self.suite, self.case, nested, self.plan)['outcome'], 'PASS')

    def test_invalid_evidence_indices_paths_and_missing_binding_fail(self):
        invalid_bindings = [()]
        invalid_bindings.extend((self.binding.model_copy(update=change),) for change in (
            {'message_index': 9}, {'json_path': ('absent',)}, {'fragment_index': 1}))
        for bindings in invalid_bindings:
            with self.subTest(bindings=bindings):
                trace = self.trace.model_copy(update={'request_evidence_bindings': bindings})
                self.assertEqual(score_case(self.suite, self.case, trace, self.plan)['outcome'], 'FAIL')

    def test_forged_role_or_changed_evidence_text_fails(self):
        for message in ({'role': 'assistant', 'content': self.messages[1]['content']},
                        {'role': 'tool', 'content': self.messages[1]['content'].replace('42', '43')}):
            with self.subTest(message=message):
                trace = self.trace.model_copy(update={'request_messages': (self.messages[0], message)})
                self.assertEqual(score_case(self.suite, self.case, trace, self.plan)['outcome'], 'FAIL')

    def test_full_wire_request_must_fit_context_budget(self):
        case = self.case.model_copy(update={'input': self.case.input.model_copy(update={'context_budget': 10})})
        self.assertIn('context_budget_exceeded', score_case(self.suite, case, self.trace, self.plan)['reasons'])


if __name__ == '__main__':
    unittest.main()
